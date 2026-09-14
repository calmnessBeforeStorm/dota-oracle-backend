"""Settings that must refuse to exist rather than run unsafely."""

import pytest
from pydantic import ValidationError

from app.core.config import DEV_SECRET_KEY, MIN_SECRET_KEY_BYTES, Settings


def test_local_runs_on_the_development_key() -> None:
    assert Settings(_env_file=None, app_env="local", secret_key=DEV_SECRET_KEY).secret_key


@pytest.mark.parametrize(
    "key",
    [
        "",
        "x" * (MIN_SECRET_KEY_BYTES - 1),
        # The default is in a public repository: anyone could mint tokens with it.
        DEV_SECRET_KEY,
        # 15 two-byte characters: 15 characters, 30 bytes. The limit is on bytes.
        "ж" * 15,
    ],
)
def test_production_refuses_an_unsafe_key(key: str) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(_env_file=None, app_env="production", secret_key=key)


def test_production_accepts_a_long_key() -> None:
    key = "x" * MIN_SECRET_KEY_BYTES
    assert Settings(_env_file=None, app_env="production", secret_key=key).secret_key == key


def test_the_refusal_does_not_print_the_key() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, app_env="production", secret_key="short-but-secret")
    assert "short-but-secret" not in str(caught.value)
