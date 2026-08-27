"""F5: live probability updates over WebSocket (spec sections 8.1, 10).

The poller publishes to Redis pub/sub; this endpoint fans out to connected browsers.

Mounted at the application root, not under `/api` - see the note in app/api/router.py.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.logging import get_logger
from app.core.redis import subscribe_predictions

router = APIRouter(tags=["live"])
log = get_logger(__name__)


@router.websocket("/ws/live/{match_id}")
async def live_updates(websocket: WebSocket, match_id: int) -> None:
    """Stream this match's predictions until the client leaves.

    A live match can go a minute between updates, so the subscription yields an idle tick
    and the socket is pinged. That ping is not decoration: it is how a browser that has
    closed the tab gets noticed, and without it the connection would keep the server from
    shutting down.
    """
    await websocket.accept()
    try:
        async for payload in subscribe_predictions(match_id):
            if payload is None:
                # Raises as soon as the client is gone, which ends the subscription.
                await websocket.send_text('{"type":"ping"}')
            else:
                await websocket.send_text(payload)
    except WebSocketDisconnect:
        log.info("ws.disconnect", match_id=match_id)
    except Exception as exc:
        log.info("ws.closed", match_id=match_id, reason=type(exc).__name__)
