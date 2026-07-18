"""Tests for the Chronos inbound cron-fire JWT verifier (Phase 4E.1).

These exercise REAL RS256 signing/verification (PyJWT[crypto] is a declared
dependency) against an inline PEM public key — no mocking of the crypto, since
this is a security boundary. The JWKS-URL path is covered separately by mocking
PyJWKClient's key resolution.
"""

import concurrent.futures
import time

import pytest


@pytest.fixture(autouse=True)
def _reset_jwks_state():
    import plugins.cron_providers.chronos.verify as verify

    cache_clear = verify._get_jwk_client.cache_clear
    cache_clear()
    verify._JWKS_LAST_REFRESH.clear()
    verify._JWKS_LAST_FETCH_FAILURE.clear()
    verify._JWKS_SETS.clear()
    yield
    cache_clear()
    verify._JWKS_LAST_REFRESH.clear()
    verify._JWKS_LAST_FETCH_FAILURE.clear()
    verify._JWKS_SETS.clear()


@pytest.fixture(scope="module")
def rsa_keys():
    """An RS256 keypair: (private_pem, public_pem)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pub


def _mint(priv, claims):
    import jwt
    return jwt.encode(claims, priv, algorithm="RS256")


AUD = "agent:inst-123"
ISS = "https://portal.nousresearch.com"


def _base_claims(**over):
    now = int(time.time())
    c = {
        "aud": AUD,
        "iss": ISS,
        "purpose": "cron_fire",
        "iat": now,
        "nbf": now - 5,
        "exp": now + 300,
    }
    c.update(over)
    return c


def test_valid_token_returns_claims(rsa_keys):
    from plugins.cron_providers.chronos.verify import verify_nas_fire_token

    priv, pub = rsa_keys
    token = _mint(priv, _base_claims())
    claims = verify_nas_fire_token(token=token, expected_audience=AUD,
                                   jwks_or_key=pub, issuer=ISS)
    assert claims is not None
    assert claims["purpose"] == "cron_fire"
    assert claims["aud"] == AUD


def test_wrong_audience_rejected(rsa_keys):
    from plugins.cron_providers.chronos.verify import verify_nas_fire_token

    priv, pub = rsa_keys
    token = _mint(priv, _base_claims(aud="agent:someone-else"))
    assert verify_nas_fire_token(token=token, expected_audience=AUD,
                                 jwks_or_key=pub, issuer=ISS) is None


def test_missing_purpose_rejected(rsa_keys):
    """A general agent JWT (no purpose=cron_fire) can't fire jobs."""
    from plugins.cron_providers.chronos.verify import verify_nas_fire_token

    priv, pub = rsa_keys
    claims = _base_claims()
    del claims["purpose"]
    token = _mint(priv, claims)
    assert verify_nas_fire_token(token=token, expected_audience=AUD,
                                 jwks_or_key=pub, issuer=ISS) is None


def test_missing_nbf_rejected(rsa_keys):
    from plugins.cron_providers.chronos.verify import verify_nas_fire_token

    priv, pub = rsa_keys
    claims = _base_claims()
    del claims["nbf"]
    token = _mint(priv, claims)
    assert verify_nas_fire_token(
        token=token,
        expected_audience=AUD,
        jwks_or_key=pub,
        issuer=ISS,
    ) is None


def test_wrong_purpose_rejected(rsa_keys):
    from plugins.cron_providers.chronos.verify import verify_nas_fire_token

    priv, pub = rsa_keys
    token = _mint(priv, _base_claims(purpose="inference"))
    assert verify_nas_fire_token(token=token, expected_audience=AUD,
                                 jwks_or_key=pub, issuer=ISS) is None


def test_expired_token_rejected(rsa_keys):
    from plugins.cron_providers.chronos.verify import verify_nas_fire_token

    priv, pub = rsa_keys
    now = int(time.time())
    token = _mint(priv, _base_claims(iat=now - 1000, nbf=now - 1000, exp=now - 600))
    assert verify_nas_fire_token(token=token, expected_audience=AUD,
                                 jwks_or_key=pub, issuer=ISS) is None


def test_wrong_issuer_rejected(rsa_keys):
    from plugins.cron_providers.chronos.verify import verify_nas_fire_token

    priv, pub = rsa_keys
    token = _mint(priv, _base_claims(iss="https://evil.example"))
    assert verify_nas_fire_token(token=token, expected_audience=AUD,
                                 jwks_or_key=pub, issuer=ISS) is None


def test_tampered_signature_rejected(rsa_keys):
    """A token signed by a DIFFERENT key must fail signature verification."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from plugins.cron_providers.chronos.verify import verify_nas_fire_token

    _, pub = rsa_keys
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attacker_priv = attacker.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    token = _mint(attacker_priv, _base_claims())
    # Verified against the REAL public key → signature mismatch → None.
    assert verify_nas_fire_token(token=token, expected_audience=AUD,
                                 jwks_or_key=pub, issuer=ISS) is None


def test_no_key_configured_refuses(rsa_keys):
    """No JWKS/key configured → refuse (never fall back to unsigned decode)."""
    from plugins.cron_providers.chronos.verify import verify_nas_fire_token

    priv, _ = rsa_keys
    token = _mint(priv, _base_claims())
    assert verify_nas_fire_token(token=token, expected_audience=AUD,
                                 jwks_or_key=None) is None


def test_empty_token_refused(rsa_keys):
    from plugins.cron_providers.chronos.verify import verify_nas_fire_token

    _, pub = rsa_keys
    assert verify_nas_fire_token(token="", expected_audience=AUD, jwks_or_key=pub) is None


def test_jwks_url_path_resolves_key(rsa_keys, monkeypatch):
    """The JWKS-URL branch uses a cached, timeout-bounded PyJWKClient."""
    from plugins.cron_providers.chronos.verify import (
        _JWKS_LAST_FETCH_FAILURE,
        _JWKS_LAST_REFRESH,
        _get_jwk_client,
        verify_nas_fire_token,
    )

    import jwt

    _get_jwk_client.cache_clear()
    _JWKS_LAST_REFRESH.clear()
    _JWKS_LAST_FETCH_FAILURE.clear()
    priv, pub = rsa_keys
    token = jwt.encode(
        _base_claims(),
        priv,
        algorithm="RS256",
        headers={"kid": "nas-key-1"},
    )

    class FakeKey:
        key_id = "nas-key-1"
        key = pub

    class FakeJWKSet:
        keys = [FakeKey()]

    clients = []

    class FakeJWKClient:
        def __init__(self, url, *, timeout):
            assert url == "https://portal.nousresearch.com/.well-known/jwks.json"
            assert timeout == 5
            clients.append(self)

        def get_jwk_set(self, *, refresh=False):
            assert refresh is False
            return FakeJWKSet()

    monkeypatch.setattr("jwt.PyJWKClient", FakeJWKClient)
    kwargs = {
        "token": token,
        "expected_audience": AUD,
        "jwks_or_key": "https://portal.nousresearch.com/.well-known/jwks.json",
        "issuer": ISS,
    }
    claims = verify_nas_fire_token(**kwargs)
    assert claims is not None and claims["purpose"] == "cron_fire"
    assert verify_nas_fire_token(**kwargs) is not None
    assert len(clients) == 1
    _get_jwk_client.cache_clear()


def test_jwks_rotation_gets_one_rate_limited_refresh(rsa_keys, monkeypatch):
    import jwt
    import plugins.cron_providers.chronos.verify as verify

    verify._get_jwk_client.cache_clear()
    verify._JWKS_LAST_REFRESH.clear()
    verify._JWKS_LAST_FETCH_FAILURE.clear()
    priv, pub = rsa_keys
    token = jwt.encode(
        _base_claims(),
        priv,
        algorithm="RS256",
        headers={"kid": "rotated-key"},
    )

    class Key:
        key_id = "rotated-key"
        public_key_use = "sig"
        key = pub

    class Set:
        def __init__(self, keys):
            self.keys = keys

    refreshes = []

    class Client:
        def get_jwk_set(self, *, refresh=False):
            refreshes.append(refresh)
            return Set([Key()] if refresh else [])

    monkeypatch.setattr(verify, "_get_jwk_client", lambda url: Client())
    monkeypatch.setattr(verify.time, "monotonic", lambda: 1.0)

    assert verify.verify_nas_fire_token(
        token=token,
        expected_audience=AUD,
        jwks_or_key="https://portal.test/.well-known/jwks.json",
        issuer=ISS,
    ) is not None
    assert refreshes == [False, True]
    verify._JWKS_LAST_REFRESH.clear()
    verify._JWKS_LAST_FETCH_FAILURE.clear()


def test_concurrent_jwks_fetch_failures_make_one_network_attempt(
    rsa_keys,
    monkeypatch,
):
    import jwt
    import plugins.cron_providers.chronos.verify as verify

    verify._get_jwk_client.cache_clear()
    verify._JWKS_LAST_REFRESH.clear()
    verify._JWKS_LAST_FETCH_FAILURE.clear()
    priv, _pub = rsa_keys
    token = jwt.encode(
        _base_claims(),
        priv,
        algorithm="RS256",
        headers={"kid": "unknown-key"},
    )
    attempts = []

    class Client:
        def get_jwk_set(self, *, refresh=False):
            attempts.append(refresh)
            time.sleep(0.05)
            raise OSError("JWKS unavailable")

    monkeypatch.setattr(verify, "_get_jwk_client", lambda url: Client())
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        results = list(
            executor.map(
                lambda _index: verify.verify_nas_fire_token(
                    token=token,
                    expected_audience=AUD,
                    jwks_or_key="https://portal.test/.well-known/jwks.json",
                    issuer=ISS,
                ),
                range(6),
            )
        )

    assert results == [None] * 6
    assert attempts == [False]
    verify._JWKS_LAST_FETCH_FAILURE.clear()


def test_failed_unknown_key_refresh_keeps_cached_signing_keys(
    rsa_keys,
    monkeypatch,
):
    import jwt
    import plugins.cron_providers.chronos.verify as verify

    priv, pub = rsa_keys
    known_token = jwt.encode(
        _base_claims(),
        priv,
        algorithm="RS256",
        headers={"kid": "known-key"},
    )
    unknown_token = jwt.encode(
        _base_claims(),
        priv,
        algorithm="RS256",
        headers={"kid": "unknown-key"},
    )

    class Key:
        key_id = "known-key"
        public_key_use = "sig"
        key = pub

    class Set:
        keys = [Key()]

    class Client:
        def get_jwk_set(self, *, refresh=False):
            if refresh:
                raise OSError("refresh unavailable")
            return Set()

    monkeypatch.setattr(verify, "_get_jwk_client", lambda url: Client())
    assert verify.verify_nas_fire_token(
        token=unknown_token,
        expected_audience=AUD,
        jwks_or_key="https://portal.test/.well-known/jwks.json",
        issuer=ISS,
    ) is None
    assert verify.verify_nas_fire_token(
        token=known_token,
        expected_audience=AUD,
        jwks_or_key="https://portal.test/.well-known/jwks.json",
        issuer=ISS,
    ) is not None


def test_encryption_only_jwk_is_not_accepted():
    from plugins.cron_providers.chronos.verify import _find_signing_key

    class EncryptionKey:
        key_id = "shared-kid"
        public_key_use = "enc"
        key = object()

    class Set:
        keys = [EncryptionKey()]

    assert _find_signing_key(Set(), "shared-kid") is None


def test_http_jwks_url_is_rejected_before_network(rsa_keys, monkeypatch):
    from plugins.cron_providers.chronos.verify import verify_nas_fire_token

    priv, _pub = rsa_keys
    token = _mint(priv, _base_claims())
    monkeypatch.setattr(
        "jwt.PyJWKClient",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("HTTP JWKS must not be fetched")
        ),
    )

    assert verify_nas_fire_token(
        token=token,
        expected_audience=AUD,
        jwks_or_key="http://portal.test/.well-known/jwks.json",
        issuer=ISS,
    ) is None


def test_get_fire_verifier_returns_nas_verifier():
    from plugins.cron_providers.chronos.verify import get_fire_verifier, verify_nas_fire_token

    assert get_fire_verifier() is verify_nas_fire_token
