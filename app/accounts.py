"""Accounts, sessions, and the per-user secret the Shortcut sends.

Password hashing and session signing both use the standard library. A service
this size should not take a dependency to do what `hashlib.scrypt` and `hmac`
already do correctly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

# scrypt at these parameters costs ~100ms and ~64MB per verification, which is
# the point: it makes a stolen table expensive to attack while staying
# unnoticeable on a login. Stored with the parameters so they can be raised
# later without invalidating existing passwords.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
# OpenSSL caps scrypt at 32MB by default, which is exactly what these
# parameters need, so it has to be raised or every hash raises.
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2
SESSION_TTL = 60 * 60 * 24 * 30


class AuthError(RuntimeError):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=32, maxmem=SCRYPT_MAXMEM,
    )
    return "$".join((
        "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    ))


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
            maxmem=128 * int(n) * int(r) * 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def new_api_key() -> str:
    """The per-account secret the Shortcut sends as X-API-Key."""
    return secrets.token_urlsafe(32)


def sign_session(user_id: str, secret: str, *, now: float | None = None) -> str:
    """A signed, expiring session token.

    Self-contained rather than a row in a table: nothing to clean up, and no
    database read on the hot path of every single request.
    """
    payload = {"u": user_id, "exp": int((now or time.time()) + SESSION_TTL)}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    return f"{raw.decode()}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


def read_session(token: str, secret: str, *, now: float | None = None) -> str | None:
    """The user id a token vouches for, or None if it does not."""
    try:
        raw_str, sig_str = token.split(".", 1)
        raw = raw_str.encode()
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
        given = base64.urlsafe_b64decode(sig_str + "=" * (-len(sig_str) % 4))
        # Compare before parsing: an unsigned payload is not worth reading.
        if not hmac.compare_digest(expected, given):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4)))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < (now or time.time()):
        return None
    user = payload.get("u")
    return user if isinstance(user, str) else None


def normalise_email(email: str) -> str:
    return email.strip().lower()


def check_password_strength(password: str) -> None:
    """Refuse the passwords that make an account not worth having."""
    if len(password) < 10:
        raise AuthError("Use at least 10 characters.")
    if password.strip() != password:
        raise AuthError("Remove the spaces at the start or end.")
