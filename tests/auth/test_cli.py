"""User management from the command line (design, section 1)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import cli
from app.auth.passwords import verify_password
from app.auth.permissions import RUNS_VIEW, known_permissions
from app.db.models.auth import AuthSession, User
from tests.auth.conftest import MakeUser

Sessions = async_sessionmaker[AsyncSession]
NEW_PASSWORD = "a brand new passphrase"


def _prompt(*answers: str) -> cli.PasswordPrompt:
    replies: Iterator[str] = iter(answers)
    return lambda _label: next(replies)


async def _user(sessions: Sessions, username: str = "adilet") -> User:
    async with sessions() as session:
        user = await session.scalar(select(User).where(User.username == username))
    assert user is not None
    return user


async def _add_session(sessions: Sessions, user: User) -> None:
    async with sessions() as session:
        session.add(
            AuthSession(
                user_id=user.id, token_hash="h", expires_at=datetime.now(UTC) + timedelta(days=1)
            )
        )
        await session.commit()


async def _live_sessions(sessions: Sessions) -> int:
    async with sessions() as session:
        rows = await session.scalars(select(AuthSession).where(AuthSession.revoked_at.is_(None)))
        return len(list(rows))


class TestReadNewPassword:
    def test_returns_a_password_typed_twice(self) -> None:
        assert cli.read_new_password(_prompt(NEW_PASSWORD, NEW_PASSWORD)) == NEW_PASSWORD

    def test_refuses_a_mismatch(self) -> None:
        with pytest.raises(cli.UsageError, match="do not match"):
            cli.read_new_password(_prompt(NEW_PASSWORD, NEW_PASSWORD + "x"))

    def test_refuses_a_short_password(self) -> None:
        short = "x" * (cli.MIN_PASSWORD_LENGTH - 1)
        with pytest.raises(cli.UsageError, match="at least"):
            cli.read_new_password(_prompt(short, short))


class TestCreateUser:
    async def test_grants_every_known_permission(self, sessionmaker: Sessions) -> None:
        await cli.create_user(
            sessionmaker, username="adilet", display_name="Adilet", password=NEW_PASSWORD
        )
        user = await _user(sessionmaker)
        assert user.display_name == "Adilet"
        assert user.permissions == list(known_permissions())
        assert verify_password(user.password_hash, NEW_PASSWORD)

    async def test_refuses_a_taken_username(self, sessionmaker: Sessions) -> None:
        await cli.create_user(
            sessionmaker, username="adilet", display_name="Adilet", password=NEW_PASSWORD
        )
        with pytest.raises(cli.UsageError, match="already exists"):
            await cli.create_user(
                sessionmaker, username="adilet", display_name="Other", password=NEW_PASSWORD
            )


class TestSetPassword:
    async def test_changes_the_password_and_signs_out_everywhere(
        self, sessionmaker: Sessions, make_user: MakeUser
    ) -> None:
        user = await make_user()
        await _add_session(sessionmaker, user)
        await _add_session(sessionmaker, user)

        revoked = await cli.set_password(sessionmaker, username="adilet", password=NEW_PASSWORD)

        assert revoked == 2
        assert await _live_sessions(sessionmaker) == 0
        assert verify_password((await _user(sessionmaker)).password_hash, NEW_PASSWORD)

    async def test_refuses_an_unknown_user(self, sessionmaker: Sessions) -> None:
        with pytest.raises(cli.UsageError, match="no user"):
            await cli.set_password(sessionmaker, username="nobody", password=NEW_PASSWORD)


class TestRevokeSessions:
    async def test_revokes_only_that_users_sessions(
        self, sessionmaker: Sessions, make_user: MakeUser
    ) -> None:
        adilet = await make_user()
        other = await make_user(username="other")
        await _add_session(sessionmaker, adilet)
        await _add_session(sessionmaker, other)

        assert await cli.revoke_sessions(sessionmaker, username="adilet") == 1
        assert await _live_sessions(sessionmaker) == 1


class TestGrantAll:
    async def test_adds_only_the_missing_codes(
        self, sessionmaker: Sessions, make_user: MakeUser
    ) -> None:
        await make_user(permissions=[RUNS_VIEW])
        added = await cli.grant_all(sessionmaker, username="adilet")
        assert added == [code for code in known_permissions() if code != RUNS_VIEW]
        assert (await _user(sessionmaker)).permissions == [RUNS_VIEW, *added]

    async def test_says_so_when_nothing_is_missing(
        self, sessionmaker: Sessions, make_user: MakeUser
    ) -> None:
        await make_user(permissions=list(known_permissions()))
        assert await cli.grant_all(sessionmaker, username="adilet") == []


def test_the_password_is_never_an_argument() -> None:
    """A password in argv stays in shell history and in the process list."""
    parser = cli.build_parser()
    for command in ("create-user", "set-password"):
        args = ["--username", "adilet"]
        if command == "create-user":
            args += ["--display-name", "Adilet"]
        with pytest.raises(SystemExit):
            parser.parse_args([command, *args, "--password", "x"])
