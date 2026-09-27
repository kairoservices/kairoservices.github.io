"""Entry point: `python -m bot`."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from bot.chain.deployer import TokenDeployer
from bot.config import load_config
from bot.handlers import build_router
from bot.middlewares import AllowlistMiddleware
from bot.services.price import SolPriceFeed
from bot.services.storage import PinataStorage

log = logging.getLogger("bot")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()

    deployer = TokenDeployer(
        config.rpc_url,
        config.payer,
        priority_fee_micro_lamports=config.priority_fee_micro_lamports,
        min_payer_balance_lamports=config.min_payer_balance_lamports,
        pump_fee_bps=config.pump_fee_bps,
        pump_slippage_bps=config.pump_slippage_bps,
        pump_lookup_table=config.pump_lookup_table,
        # Persist so restarts reuse the table instead of paying rent for a new one.
        on_lookup_table_created=lambda address: config.pump_lookup_table_file.write_text(str(address)),
    )
    ipfs = PinataStorage(config.pinata_jwt, config.pinata_gateway)
    prices = SolPriceFeed()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # MemoryStorage loses in-progress flows on restart; swap for RedisStorage in multi-instance setups.
    dp = Dispatcher(storage=MemoryStorage(), config=config, deployer=deployer, ipfs=ipfs, prices=prices)

    allowlist = AllowlistMiddleware(config.allowed_user_ids)
    dp.message.outer_middleware(allowlist)
    dp.callback_query.outer_middleware(allowlist)
    dp.include_router(build_router())

    await bot.set_my_commands(
        [
            BotCommand(command="launch", description="Create a new token"),
            BotCommand(command="cancel", description="Abort current flow"),
            BotCommand(command="help", description="Help"),
        ]
    )

    log.info("Payer wallet: %s | cluster: %s", deployer.payer_pubkey, config.cluster)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await deployer.close()
        await ipfs.close()
        await prices.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
