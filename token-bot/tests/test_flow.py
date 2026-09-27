"""End-to-end conversation tests with a fake Telegram session and fake chain/IPFS services."""

from __future__ import annotations

import datetime
import itertools
import unittest
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, DeleteMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from bot.chain import pump
from bot.chain.deployer import DeploymentError
from bot.handlers import build_router
from bot.middlewares import AllowlistMiddleware
from bot.models import DeployResult, PumpDeployResult

CHAT = Chat(id=1, type="private")
USER = User(id=42, is_bot=False, first_name="dev")
GLOBAL = pump.PumpGlobal(
    Pubkey.default(), [], 1_073_000_000_000_000, 30_000_000_000, 793_100_000_000_000, 10**15, 95, 30, True, [], 0
)


def _build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    allowlist = AllowlistMiddleware(frozenset({USER.id}))
    dp.message.outer_middleware(allowlist)
    dp.callback_query.outer_middleware(allowlist)
    dp.include_router(build_router())
    return dp


_DP = _build_dispatcher()


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[Any] = []

    async def make_request(self, bot, method, timeout=None):  # type: ignore[override]
        self.calls.append(method)
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage)):
            return True
        return Message(message_id=99, date=datetime.datetime.now(), chat=CHAT, text="x")

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):  # type: ignore[override]
        yield b""


class FakeDeployer:
    def __init__(self, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.params: Any = None

    async def deploy(self, p):
        self.params = p
        if self.fail_first:
            self.fail_first = False
            raise DeploymentError("Payer wallet has 0 SOL")
        return DeployResult(mint=Keypair().pubkey(), signature="sig")

    async def fetch_pump_global(self):
        return GLOBAL

    async def deploy_pump(self, p):
        self.params = p
        tokens = pump.quote_first_buy(GLOBAL, p.dev_buy_lamports, 300)
        return PumpDeployResult(
            Keypair().pubkey(), "sig", tokens, GLOBAL.initial_market_cap_lamports,
            pump.market_cap_after_first_buy(GLOBAL, tokens), 0.01,
        )


class FakeIpfs:
    async def upload_json(self, content, name):
        self.content = content
        return "https://gw/ipfs/Qm"


class FakePrices:
    async def usd(self):
        return 150.0


class FakeConfig:
    is_mainnet = False
    cluster = "devnet"
    pump_enabled = True
    pump_fee_bps = 300
    pump_max_dev_buy_lamports = 5 * 10**9


class FlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.ids = itertools.count(1)
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))

    def make_dp(self, deployer: FakeDeployer) -> Dispatcher:
        # Routers are module-level singletons, so one Dispatcher is shared; reset its state per test.
        _DP.fsm.storage = MemoryStorage()
        _DP.workflow_data.update(config=FakeConfig(), deployer=deployer, ipfs=FakeIpfs(), prices=FakePrices())
        return _DP

    def msg(self, text: str) -> Update:
        return Update(
            update_id=next(self.ids),
            message=Message(
                message_id=next(self.ids), date=datetime.datetime.now(), chat=CHAT, from_user=USER, text=text
            ),
        )

    def cb(self, data: str, user: User = USER) -> Update:
        return Update(
            update_id=next(self.ids),
            callback_query=CallbackQuery(
                id=str(next(self.ids)), from_user=user, chat_instance="c", data=data,
                message=Message(message_id=5, date=datetime.datetime.now(), chat=CHAT, text="x"),
            ),
        )

    async def run_steps(self, dp: Dispatcher, steps: list[Update]) -> None:
        for update in steps:
            await dp.feed_update(self.bot, update)

    def texts(self) -> list[str]:
        return [getattr(c, "text", "") or "" for c in self.session.calls]

    async def test_spl_flow_with_retry(self) -> None:
        deployer = FakeDeployer(fail_first=True)
        dp = self.make_dp(deployer)
        recipient = str(Keypair().pubkey())
        await self.run_steps(dp, [
            self.msg("/launch"), self.msg("My Token"), self.msg("$moon"), self.cb("skip"),
            self.msg("https://img.example/x.png"), self.cb("mode:spl"), self.cb("tok:custom"),
            self.msg("6"), self.msg("1,000,000"), self.msg(recipient),
            self.cb("auth:mint"), self.cb("auth:done"), self.cb("confirm:deploy"), self.cb("confirm:deploy"),
        ])
        self.assertTrue(any("Deployment failed" in t for t in self.texts()))
        self.assertIn("Your token has been created!", self.texts()[-1])
        p = deployer.params
        self.assertEqual((p.symbol, p.decimals, p.supply), ("MOON", 6, 1_000_000))
        self.assertEqual((p.revoke_mint, p.revoke_freeze, p.revoke_update), (False, True, False))
        self.assertIsNone(await dp.fsm.get_context(self.bot, CHAT.id, USER.id).get_state())

    async def test_pump_flow_with_dev_buy(self) -> None:
        deployer = FakeDeployer()
        dp = self.make_dp(deployer)
        await self.run_steps(dp, [
            self.msg("/launch"), self.msg("Moon Cat"), self.msg("MCAT"), self.msg("best cat"), self.cb("skip"),
            self.cb("mode:pump"), self.msg(str(Keypair().pubkey())),
            self.msg("abc"), self.msg("9"),  # rejected: not a number, above max
            self.msg("0.2 SOL"), self.cb("confirm:deploy"),
        ])
        texts = self.texts()
        self.assertTrue(any("Enter initial dev buy amount in SOL" in t for t in texts))
        self.assertEqual(sum("Enter 0, or between" in t for t in texts), 2)
        self.assertTrue(any("Starting market cap" in t for t in texts))
        self.assertIn("Your token has been created!", texts[-1])
        self.assertIn("Market cap:", texts[-1])
        self.assertEqual(deployer.params.dev_buy_lamports, 200_000_000)

    async def test_unauthorized_user_blocked(self) -> None:
        dp = self.make_dp(FakeDeployer())
        stranger = User(id=7, is_bot=False, first_name="x")
        await self.run_steps(dp, [self.cb("confirm:deploy", user=stranger)])
        self.assertIn("Not authorized", self.session.calls[-1].text)


if __name__ == "__main__":
    unittest.main()
