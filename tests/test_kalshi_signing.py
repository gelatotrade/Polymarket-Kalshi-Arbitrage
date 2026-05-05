"""Tests for Kalshi RSA-PSS signing.

Generates an ephemeral RSA key in-memory and exercises the
``KalshiClient`` signing path end-to-end without hitting the
network. Verifies the signature with the public key so we know
it would be accepted by Kalshi's verifier.
"""

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.api.kalshi_client import KalshiClient, _load_rsa_private_key


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_key, pem


def test_load_rsa_private_key_from_pem(rsa_keypair):
    _, pem = rsa_keypair
    key = _load_rsa_private_key(key_pem=pem.decode())
    assert isinstance(key, rsa.RSAPrivateKey)


def test_load_rsa_private_key_returns_none_when_missing():
    assert _load_rsa_private_key() is None


def test_load_rsa_private_key_returns_none_for_garbage():
    assert _load_rsa_private_key(key_pem="not a real pem") is None


def test_signature_matches_kalshi_spec(rsa_keypair):
    private_key, pem = rsa_keypair
    client = KalshiClient(api_key="test-key", private_key_pem=pem.decode())

    timestamp = "1700000000000"
    method = "GET"
    path = "/trade-api/v2/markets"
    signature_b64 = client._generate_signature(timestamp, method, path)

    # Independent verification with the public key.
    public_key = private_key.public_key()
    message = f"{timestamp}{method}{path}".encode()
    public_key.verify(
        base64.b64decode(signature_b64),
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


def test_headers_include_signature_when_authenticated(rsa_keypair):
    _, pem = rsa_keypair
    client = KalshiClient(api_key="abc", private_key_pem=pem.decode())
    headers = client._get_headers("GET", "/markets")
    assert headers["KALSHI-ACCESS-KEY"] == "abc"
    assert "KALSHI-ACCESS-TIMESTAMP" in headers
    assert "KALSHI-ACCESS-SIGNATURE" in headers
    assert len(headers["KALSHI-ACCESS-SIGNATURE"]) > 0


def test_headers_skip_signature_without_key():
    client = KalshiClient(api_key="abc")  # no private key
    headers = client._get_headers("GET", "/markets")
    assert "KALSHI-ACCESS-SIGNATURE" not in headers
    assert "KALSHI-ACCESS-KEY" not in headers


def test_path_for_signature_includes_base_prefix():
    client = KalshiClient(
        api_key="abc",
        base_url="https://api.elections.kalshi.com/trade-api/v2",
    )
    assert client._path_for_signature("/markets") == "/trade-api/v2/markets"
