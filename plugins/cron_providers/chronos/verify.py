"""Inbound cron-fire token verification for Chronos (Phase 4E.1).

When NAS relays an external scheduler fire to the agent, it POSTs
``/api/cron/fire`` with a short-lived NAS-minted JWT. This module verifies that
JWT before any job runs — the security boundary for remotely-triggered job
execution.

We verify a NAS-minted JWT (the trust path the agent already has) rather than
let an external scheduler call the agent directly: the scheduler signs with
NAS's keys, which the agent doesn't (and shouldn't) hold. See the plan's DQ-4.

The verifier is pluggable (``get_fire_verifier``) so the escape-hatch mode
(direct per-job cron-key) can swap in later with no handler change.

Crypto is delegated to PyJWT (already a declared dependency) — we do NOT
hand-roll JWT verification.
"""

from __future__ import annotations

import logging
import threading
import time
from functools import lru_cache
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("cron.chronos.verify")

# The purpose claim that scopes a token to the fire endpoint. A general agent
# JWT (without this claim) must NOT be replayable against /api/cron/fire.
_FIRE_PURPOSE = "cron_fire"
_JWKS_TIMEOUT_SECONDS = 5
_JWKS_CACHE_SECONDS = 300
_JWKS_REFRESH_INTERVAL_SECONDS = 30
_JWKS_REFRESH_LOCK = threading.Lock()
_JWKS_LAST_REFRESH: dict[str, float] = {}
_JWKS_LAST_FETCH_FAILURE: dict[str, float] = {}
_JWKS_SETS: dict[str, tuple[float, Any]] = {}


@lru_cache(maxsize=8)
def _get_jwk_client(url: str):
    """Reuse PyJWT's bounded JWKS cache for each configured NAS endpoint."""
    from jwt import PyJWKClient

    return PyJWKClient(url, timeout=_JWKS_TIMEOUT_SECONDS)


def _find_signing_key(jwk_set, key_id: str):
    for signing_key in jwk_set.keys:
        if (
            signing_key.key_id == key_id
            and getattr(signing_key, "public_key_use", None) in (None, "sig")
        ):
            return signing_key.key
    return None


def _get_cached_signing_key(url: str, token: str):
    """Resolve a key ID without allowing concurrent JWKS fetch stampedes."""
    import jwt

    key_id = jwt.get_unverified_header(token).get("kid")
    if not isinstance(key_id, str) or not key_id:
        raise ValueError("token has no key ID")

    with _JWKS_REFRESH_LOCK:
        now = time.monotonic()
        client = _get_jwk_client(url)
        cached_entry = _JWKS_SETS.get(url)
        cached = None
        if (
            cached_entry is not None
            and now - cached_entry[0] < _JWKS_CACHE_SECONDS
        ):
            cached = cached_entry[1]
            signing_key = _find_signing_key(cached, key_id)
            if signing_key is not None:
                return signing_key

        if cached is None:
            last_failure = _JWKS_LAST_FETCH_FAILURE.get(url)
            if (
                last_failure is not None
                and now - last_failure < _JWKS_REFRESH_INTERVAL_SECONDS
            ):
                raise ValueError("JWKS endpoint is temporarily unavailable")

            try:
                cached = client.get_jwk_set(refresh=False)
            except Exception:
                _JWKS_LAST_FETCH_FAILURE[url] = now
                raise
            else:
                _JWKS_LAST_FETCH_FAILURE.pop(url, None)
                _JWKS_SETS[url] = (now, cached)

            signing_key = _find_signing_key(cached, key_id)
            if signing_key is not None:
                return signing_key

        last_refresh = _JWKS_LAST_REFRESH.get(url)
        if (
            last_refresh is not None
            and now - last_refresh < _JWKS_REFRESH_INTERVAL_SECONDS
        ):
            raise ValueError("token key ID is not in the cached JWKS")
        _JWKS_LAST_REFRESH[url] = now
        try:
            refreshed = client.get_jwk_set(refresh=True)
        except Exception:
            _JWKS_LAST_FETCH_FAILURE[url] = now
            raise

        _JWKS_LAST_FETCH_FAILURE.pop(url, None)
        _JWKS_SETS[url] = (now, refreshed)
        signing_key = _find_signing_key(refreshed, key_id)
        if signing_key is None:
            raise ValueError("token key ID is not in the refreshed JWKS")
        return signing_key


def verify_nas_fire_token(
    *,
    token: str,
    expected_audience: str,
    jwks_or_key: Optional[str] = None,
    issuer: Optional[str] = None,
    leeway_seconds: int = 30,
) -> Optional[Dict[str, Any]]:
    """Verify a NAS-minted cron-fire JWT. Return decoded claims, or None.

    Checks (all must pass):
      - signature against the NAS JWKS (``jwks_or_key`` is a JWKS URL) — RS256
        family; symmetric secrets are rejected (NAS signs asymmetrically).
      - ``aud`` == ``expected_audience`` (this agent: ``agent:{instance_id}``).
      - ``exp`` / ``nbf`` within ``leeway_seconds``.
      - ``iss`` == ``issuer`` when an issuer is configured.
      - ``purpose`` == ``"cron_fire"`` — so a general agent JWT can't be
        replayed against the fire endpoint.

    Returns None (never raises) on any failure, so the handler can answer 401
    without leaking which check failed.
    """
    if not token or not expected_audience:
        return None
    if not jwks_or_key:
        # No verification key configured → cannot verify → refuse. We never
        # fall back to unsigned decode for a security boundary.
        logger.warning("cron fire: no JWKS/key configured; refusing token")
        return None

    if jwks_or_key.startswith("http://"):
        logger.warning("cron fire: refusing an unencrypted JWKS endpoint")
        return None

    try:
        import jwt

        # Resolve only a key already present in the bounded JWKS cache. Unknown
        # attacker-chosen key IDs do not trigger one network refresh per request.
        signing_key = None
        if jwks_or_key.startswith("https://"):
            signing_key = _get_cached_signing_key(jwks_or_key, token)
        else:
            # A PEM public key passed inline (test / pinned-key deployments).
            signing_key = jwks_or_key

        options = {"require": ["exp", "nbf", "aud"]}
        decode_kwargs: Dict[str, Any] = dict(
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
            audience=expected_audience,
            leeway=leeway_seconds,
            options=options,
        )
        if issuer:
            decode_kwargs["issuer"] = issuer

        claims = jwt.decode(token, signing_key, **decode_kwargs)
    except Exception as e:
        logger.warning("cron fire: token verification failed: %s", e)
        return None

    if claims.get("purpose") != _FIRE_PURPOSE:
        logger.warning("cron fire: token missing/!=%s purpose claim", _FIRE_PURPOSE)
        return None

    return claims


def get_fire_verifier() -> Callable[..., Optional[Dict[str, Any]]]:
    """Return the active inbound-fire verifier.

    Default = the NAS-JWT verifier. The DQ-4 escape hatch (direct per-job
    cron-key) would return a cron-key verifier here instead, selected by config
    — so the webhook handler never changes when the auth mode is swapped.
    """
    return verify_nas_fire_token
