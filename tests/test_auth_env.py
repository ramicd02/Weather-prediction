import base64

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from weatherbot.kalshi.auth import Signer, signing_message, signing_path
from weatherbot.kalshi.env import REST_BASE, ConfigError, load_env, resolve_env_name


def test_signing_path_drops_query_and_host():
    url = "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders?limit=5&status=resting"
    assert signing_path(url) == "/trade-api/v2/portfolio/orders"
    assert signing_path("/trade-api/v2/markets?series_ticker=KXHIGHNY") == "/trade-api/v2/markets"


def test_signing_message_includes_trade_api_prefix_and_method():
    msg = signing_message(1700000000000, "get", REST_BASE["prod"] + "/markets?limit=1")
    assert msg == "1700000000000GET/trade-api/v2/markets"


def test_signature_verifies_with_rsa_pss_sha256():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = Signer("key-123", key)
    h = signer.headers("GET", "/trade-api/v2/markets?limit=5", now_ms=1700000000000)
    assert h["KALSHI-ACCESS-KEY"] == "key-123"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000000GET/trade-api/v2/markets",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_env_defaults_to_demo():
    assert resolve_env_name({}) == "demo"
    assert resolve_env_name({"KALSHI_ENV": ""}) == "demo"
    assert resolve_env_name({"KALSHI_ENV": "demo"}) == "demo"
    assert resolve_env_name({"KALSHI_ENV": "PROD"}) == "prod"


@pytest.mark.parametrize("bad", ["production", "live", "prd", "1"])
def test_env_never_silently_falls_back(bad):
    with pytest.raises(ConfigError):
        resolve_env_name({"KALSHI_ENV": bad})


def test_load_env_requires_key_file(tmp_path):
    with pytest.raises(ConfigError):
        load_env(
            {
                "KALSHI_ENV": "prod",
                "KALSHI_API_KEY_ID": "k",
                "KALSHI_PRIVATE_KEY_PATH": str(tmp_path / "missing.pem"),
            }
        )
    pem = tmp_path / "k.pem"
    pem.write_text("x")
    env = load_env(
        {"KALSHI_ENV": "prod", "KALSHI_API_KEY_ID": "k", "KALSHI_PRIVATE_KEY_PATH": str(pem)}
    )
    assert env.rest_base == REST_BASE["prod"]
