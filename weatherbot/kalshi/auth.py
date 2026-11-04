"""Kalshi RSA-PSS request signing.

Message signed: f"{timestamp_ms}{METHOD}{path}", where path includes /trade-api/v2
(or /trade-api/ws/v2 for the WebSocket handshake) and EXCLUDES the query string.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def load_private_key(path: str | Path) -> rsa.RSAPrivateKey:
    data = Path(path).expanduser().read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("Kalshi API keys must be RSA private keys")
    return key


def signing_path(path_or_url: str) -> str:
    """Return the path to sign: no scheme/host, no query string, no fragment."""
    parts = urlsplit(path_or_url)
    return parts.path if (parts.scheme or parts.netloc) else path_or_url.split("?", 1)[0]


def signing_message(timestamp_ms: int, method: str, path_or_url: str) -> str:
    return f"{timestamp_ms}{method.upper()}{signing_path(path_or_url)}"


def sign(private_key: rsa.RSAPrivateKey, message: str) -> str:
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


class Signer:
    def __init__(self, api_key_id: str, private_key: rsa.RSAPrivateKey):
        self.api_key_id = api_key_id
        self._key = private_key

    def headers(self, method: str, path_or_url: str, now_ms: int | None = None) -> dict[str, str]:
        ts = int(time.time() * 1000) if now_ms is None else now_ms
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": sign(self._key, signing_message(ts, method, path_or_url)),
        }
