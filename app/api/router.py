"""Root API router."""

from fastapi import APIRouter

from app.api.routes import health, matches, model, teams, tournaments, ws

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(matches.router)
api_router.include_router(tournaments.router)
api_router.include_router(teams.router)
api_router.include_router(model.router)
api_router.include_router(ws.router)
