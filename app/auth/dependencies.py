"""FastAPI dependencies for protected endpoints.

Permissions are read from the access token and the database is not asked. Accepted consequence
(design, section 1): a revoked permission or session keeps working until the access token
already issued expires, at most `ACCESS_TTL`. Refresh after revocation does not succeed.

Protected endpoints take the token from the Authorization header only, never from a cookie,
which is what makes cross-site request forgery against them impossible.
"""

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.tokens import AccessClaims, InvalidTokenError, decode_access_token
from app.core.config import get_settings

_bearer = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AccessClaims:
    if credentials is None:
        raise _unauthorized()
    try:
        return decode_access_token(credentials.credentials, get_settings().secret_key)
    except InvalidTokenError:
        raise _unauthorized() from None


def require_permission(code: str) -> Callable[..., Awaitable[AccessClaims]]:
    async def dependency(claims: AccessClaims = Depends(current_claims)) -> AccessClaims:
        if code not in claims.permissions:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        return claims

    return dependency
