"""IPFS uploads via Pinata for the token logo and off-chain metadata JSON."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

_PIN_FILE_URL = "https://api.pinata.cloud/pinning/pinFileToIPFS"
_PIN_JSON_URL = "https://api.pinata.cloud/pinning/pinJSONToIPFS"


class StorageError(Exception):
    pass


class PinataStorage:
    def __init__(self, jwt: str, gateway: str) -> None:
        self._headers = {"Authorization": f"Bearer {jwt}"}
        self._gateway = gateway
        self._session: aiohttp.ClientSession | None = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._headers, timeout=aiohttp.ClientTimeout(total=60)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, url: str, **kwargs: Any) -> str:
        try:
            async with self._http().post(url, **kwargs) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise StorageError(f"Pinata returned HTTP {resp.status}: {body[:200]}")
                payload = await resp.json()
        except aiohttp.ClientError as exc:
            raise StorageError(f"Pinata request failed: {exc}") from exc
        return self._gateway + payload["IpfsHash"]

    async def upload_file(self, data: bytes, filename: str, content_type: str) -> str:
        form = aiohttp.FormData()
        form.add_field("file", data, filename=filename, content_type=content_type)
        uri = await self._post(_PIN_FILE_URL, data=form)
        log.info("Uploaded %s -> %s", filename, uri)
        return uri

    async def upload_json(self, content: dict[str, Any], name: str) -> str:
        uri = await self._post(
            _PIN_JSON_URL,
            json={"pinataContent": content, "pinataMetadata": {"name": name}},
        )
        log.info("Uploaded metadata %s -> %s", name, uri)
        return uri


def build_offchain_metadata(
    *, name: str, symbol: str, description: str, image_uri: str | None, image_mime: str | None
) -> dict[str, Any]:
    """Metaplex fungible-token JSON standard."""
    meta: dict[str, Any] = {"name": name, "symbol": symbol, "description": description}
    if image_uri:
        meta["image"] = image_uri
        meta["properties"] = {
            "files": [{"uri": image_uri, "type": image_mime or "image/png"}],
            "category": "image",
        }
    return meta
