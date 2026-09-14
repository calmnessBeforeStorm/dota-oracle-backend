"""Password hashing with Argon2id."""

from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

#: argon2-cffi defaults to Argon2id with RFC 9106 parameters.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True only for a match. A corrupt hash is a failed check, never an exception: a login
    endpoint that crashes on one row answers differently for that login, which is exactly the
    signal the uniform 401 exists to deny."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@cache
def dummy_hash() -> str:
    """A real hash of a password nobody has, checked when the login does not exist.

    Returning 401 straight away for an unknown login would answer in microseconds, and a wrong
    password takes as long as Argon2 does - the timing alone would say which logins exist.
    Cached rather than computed at import so the CLI and the worker do not pay for it.
    """
    return _hasher.hash("no user has this password; it only exists to spend time")
