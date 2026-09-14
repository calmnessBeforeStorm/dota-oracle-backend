"""Manage the users of the admin panel.

    docker compose -f docker-compose.prod.yml run --rm api \\
      python -m app.auth.cli create-user --username adilet --display-name "Adilet"

Run it without `-T` and without `</dev/null`: the password is read with getpass, which needs a
terminal. It is never an argument - that would leave it in shell history and in the process list.
"""

import argparse
import asyncio
import getpass
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.auth.permissions import known_permissions
from app.auth.service import revoke_all_sessions
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models.auth import User
from app.db.session import dispose_engine, get_session_factory

MIN_PASSWORD_LENGTH = 12

PasswordPrompt = Callable[[str], str]
Sessions = async_sessionmaker[AsyncSession]


class UsageError(Exception):
    """A mistake the operator can fix, printed without a traceback."""


def read_new_password(prompt: PasswordPrompt) -> str:
    first = prompt("New password: ")
    second = prompt("Repeat the password: ")
    if first != second:
        raise UsageError("passwords do not match")
    if len(first) < MIN_PASSWORD_LENGTH:
        raise UsageError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return first


async def _find(session: AsyncSession, username: str) -> User:
    user = await session.scalar(select(User).where(User.username == username))
    if user is None:
        raise UsageError(f"no user {username!r}")
    return user


async def create_user(
    session_factory: Sessions, *, username: str, display_name: str, password: str
) -> User:
    async with session_factory() as session:
        user = User(
            username=username,
            display_name=display_name,
            password_hash=await asyncio.to_thread(hash_password, password),
            permissions=list(known_permissions()),
        )
        session.add(user)
        try:
            await session.commit()
        except IntegrityError as exc:
            raise UsageError(f"user {username!r} already exists") from exc
        return user


async def set_password(session_factory: Sessions, *, username: str, password: str) -> int:
    """Change the password and revoke every session: whoever knew the old one is out."""
    async with session_factory() as session:
        user = await _find(session, username)
        user.password_hash = await asyncio.to_thread(hash_password, password)
        revoked = await revoke_all_sessions(session, user.id)
        await session.commit()
        return revoked


async def revoke_sessions(session_factory: Sessions, *, username: str) -> int:
    async with session_factory() as session:
        user = await _find(session, username)
        revoked = await revoke_all_sessions(session, user.id)
        await session.commit()
        return revoked


async def grant_all(session_factory: Sessions, *, username: str) -> list[str]:
    """Add the codes that appeared since the user was created. A new command's button stays
    hidden until this runs - `create-user` hands out only what existed at the time."""
    async with session_factory() as session:
        user = await _find(session, username)
        missing = [code for code in known_permissions() if code not in user.permissions]
        # A new list, not an in-place append: an ARRAY column mutated in place is not seen as
        # changed and the update would silently not happen.
        user.permissions = [*user.permissions, *missing]
        await session.commit()
        return missing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.auth.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="create a user with every known permission")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name", required=True)

    password = sub.add_parser("set-password", help="change the password, revoke all sessions")
    password.add_argument("--username", required=True)

    revoke = sub.add_parser("revoke-sessions", help="sign the user out everywhere")
    revoke.add_argument("--username", required=True)

    grant = sub.add_parser("grant-all", help="grant every permission known today")
    grant.add_argument("--username", required=True)
    return parser


async def run(args: argparse.Namespace, session_factory: Sessions, prompt: PasswordPrompt) -> None:
    if args.command == "create-user":
        user = await create_user(
            session_factory,
            username=args.username,
            display_name=args.display_name,
            password=read_new_password(prompt),
        )
        print(f"created {user.username} with {len(user.permissions)} permissions")
    elif args.command == "set-password":
        revoked = await set_password(
            session_factory, username=args.username, password=read_new_password(prompt)
        )
        print(f"password changed; {revoked} sessions revoked")
    elif args.command == "revoke-sessions":
        revoked = await revoke_sessions(session_factory, username=args.username)
        print(f"{revoked} sessions revoked")
    else:
        added = await grant_all(session_factory, username=args.username)
        if added:
            print("granted:")
            for code in added:
                print(f"  {code}")
        else:
            print("nothing to grant: the user already has every known permission")


def main() -> None:
    args = build_parser().parse_args()
    configure_logging(get_settings().log_level)

    async def go() -> None:
        try:
            await run(args, get_session_factory(), getpass.getpass)
        finally:
            await dispose_engine()

    try:
        asyncio.run(go())
    except UsageError as exc:
        raise SystemExit(f"error: {exc}") from None


if __name__ == "__main__":
    main()
