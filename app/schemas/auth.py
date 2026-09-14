"""Authentication schemas (design 2026-09-11-auth-and-pipeline-panel, section 1)."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

#: The longest username login accepts. The CLI refuses to create a longer one: a user whose name
#: the login form rejects is a user nobody can sign in as.
USERNAME_MAX_LENGTH = 64


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=USERNAME_MAX_LENGTH)
    #: Bounded because Argon2 runs over whatever arrives: a megabyte password is CPU for free.
    password: str = Field(min_length=1, max_length=1024)


class AuthUser(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str
    permissions: list[str]


class TokenResponse(BaseModel):
    """The access token only. The refresh token is in the cookie and never in a body a script
    can read."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: AuthUser
