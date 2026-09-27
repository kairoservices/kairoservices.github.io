"""SOL/USD price for display only. Failures return None; never block a launch on this."""

from __future__ import annotations

import logging
import time

import aiohttp

log = logging.getLogger(__name__)

_WSOL = "So11111111111111111111111111111111111111112"
_URL = f"https://lite-api.jup.ag/price/v3?ids={_WSOL}"
_TTL_SECONDS = 60


class SolPriceFeed:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._cached: tuple[float, float] | None = None  # (timestamp, price)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def usd(self) -> float | None:
        now = time.monotonic()
        if self._cached and now - self._cached[0] < _TTL_SECONDS:
            return self._cached[1]
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
            async with self._session.get(_URL) as resp:
                resp.raise_for_status()
                price = float((await resp.json())[_WSOL]["usdPrice"])
        except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError) as exc:
            log.warning("SOL price lookup failed: %s", exc)
            return None
        self._cached = (now, price)
        return price
