"""Authentication endpoints (design 2026-09-11-auth-and-pipeline-panel, section 1).

The access token goes out in the response body and lives in the SPA's memory. The refresh token
goes out only as an HttpOnly cookie scoped to this router's path: no script can read it and no
other endpoint ever receives it.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.dependencies import current_claims
from app.auth.tokens import ACCESS_TTL, REFRESH_TTL, AccessClaims
from app.core.config import get_settings
from app.core.redis import get_redis
from app.db.session import get_session
from app.schemas.auth import AuthUser, LoginRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
#: Only /api/auth/* receives the cookie - refresh and logout are the only readers.
COOKIE_PATH = "/api/auth"
#: One text for an unknown login and a wrong password: two texts would list the logins.
INVALID_CREDENTIALS = "invalid username or password"
SESSION_REJECTED = "session expired"


def _secure() -> bool:
    # Browsers drop a Secure cookie set over plain http, and localhost is plain http.
    return get_settings().app_env == "production"


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=int(REFRESH_TTL.total_seconds()),
        path=COOKIE_PATH,
        secure=_secure(),
        httponly=True,
        # Strict, not Lax: the SPA and the API share one origin, so nothing legitimate is
        # cross-site and Strict costs nothing.
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE, path=COOKIE_PATH, secure=_secure(), httponly=True, samesite="strict"
    )


def _client_ip(request: Request) -> str:
    # Already the X-Forwarded-For address: uvicorn rewrites request.client behind Caddy.
    return request.client.host if request.client else "unknown"


def _issued(issued: service.IssuedTokens) -> JSONResponse:
    body = TokenResponse(
        access_token=issued.access_token,
        expires_in=int(ACCESS_TTL.total_seconds()),
        user=AuthUser(
            id=issued.user.id,
            username=issued.user.username,
            display_name=issued.user.display_name,
            permissions=list(issued.user.permissions),
        ),
    )
    response = JSONResponse(body.model_dump(mode="json"))
    _set_refresh_cookie(response, issued.refresh_token)
    return response


def _rejected() -> JSONResponse:
    response = JSONResponse({"detail": SESSION_REJECTED}, status_code=status.HTTP_401_UNAUTHORIZED)
    _clear_refresh_cookie(response)
    return response


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> Response:
    try:
        issued = await service.login(
            session,
            redis,
            username=body.username,
            password=body.password,
            ip=_client_ip(request),
            secret=get_settings().secret_key,
        )
    except service.LoginLockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed attempts",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except service.InvalidCredentialsError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS
        ) from None
    return _issued(issued)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    refresh_token: Annotated[str | None, Cookie()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if not refresh_token:
        return _rejected()
    try:
        issued = await service.refresh(
            session,
            refresh_token=refresh_token,
            ip=_client_ip(request),
            secret=get_settings().secret_key,
        )
    except service.RefreshRejectedError:
        return _rejected()
    return _issued(issued)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout(
    refresh_token: Annotated[str | None, Cookie()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.logout(session, refresh_token=refresh_token, secret=get_settings().secret_key)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response)
    return response


@router.get("/me", response_model=AuthUser)
async def me(claims: AccessClaims = Depends(current_claims)) -> AuthUser:
    return AuthUser(
        id=claims.user_id,
        username=claims.username,
        display_name=claims.display_name,
        permissions=list(claims.permissions),
    )
