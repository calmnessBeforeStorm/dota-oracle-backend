"""F5: live probability updates over WebSocket (spec sections 8.1, 10).

The poller publishes to Redis pub/sub; this endpoint fans out to connected browsers.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.logging import get_logger
from app.core.redis import subscribe_predictions

router = APIRouter(tags=["live"])
log = get_logger(__name__)


@router.websocket("/ws/live/{match_id}")
async def live_updates(websocket: WebSocket, match_id: int) -> None:
    await websocket.accept()
    try:
        async for payload in subscribe_predictions(match_id):
            await websocket.send_text(payload)
    except WebSocketDisconnect:
        log.info("ws.disconnect", match_id=match_id)
