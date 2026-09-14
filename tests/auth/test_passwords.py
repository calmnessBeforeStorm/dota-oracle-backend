"""Password hashing (design 2026-09-11-auth-and-pipeline-panel, section 1)."""

from app.auth.passwords import dummy_hash, hash_password, verify_password


def test_hashes_are_argon2id() -> None:
    assert hash_password("correct horse battery staple").startswith("$argon2id$")


def test_the_same_password_hashes_differently_each_time() -> None:
    assert hash_password("correct horse battery staple") != hash_password(
        "correct horse battery staple"
    )


def test_the_right_password_verifies() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password(stored, "correct horse battery staple") is True


def test_a_wrong_password_does_not_verify() -> None:
    assert verify_password(hash_password("correct horse battery staple"), "wrong") is False


def test_a_corrupt_hash_is_a_failed_check_not_a_crash() -> None:
    assert verify_password("not-a-hash", "anything") is False


def test_the_dummy_hash_is_a_real_hash_nobody_can_match() -> None:
    assert dummy_hash().startswith("$argon2id$")
    assert verify_password(dummy_hash(), "") is False
