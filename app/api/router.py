"""Root API router.

The WebSocket router is mounted at the application root rather than under `/api`, because
that is where the deployment expects it: nginx proxies `/ws/` with the upgrade headers a
socket needs and `/api/` without them, and the SPA connects to `/ws/live/{id}`.
"""

from fastapi import APIRouter

from app.api.routes import health, matches, model, teams, tournaments, ws

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(matches.router)
api_router.include_router(tournaments.router)
api_router.include_router(teams.router)
api_router.include_router(model.router)

#: Mounted separately, at the root. Included under `/api` it answered on `/api/ws/live/{id}`
#: while every client asked for `/ws/live/{id}` and got a 403 - live updates silently never
#: arrived, because a WebSocket that fails to connect looks exactly like one with nothing
#: to say.
ws_router = ws.router
