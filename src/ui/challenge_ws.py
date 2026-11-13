"""WebSocket & HTTP Status Bridge for Security Challenge Alerts in JobApplier-Core.

Exposes ``/ws/challenges``, ``GET /api/challenges/status``,
``POST /api/challenges/notify``, and ``POST /api/challenges/resolve`` for
real-time browser CAPTCHA / bot-challenge banners in the HITL verification UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Final

from fastapi import APIRouter, Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

EVENT_SECURITY_CHALLENGE_DETECTED: Final[str] = "EVENT_SECURITY_CHALLENGE_DETECTED"
EVENT_SECURITY_CHALLENGE_RESOLVED: Final[str] = "EVENT_SECURITY_CHALLENGE_RESOLVED"
DEFAULT_CHALLENGE_MESSAGE: Final[str] = "CAPTCHA Detected: Complete in Browser"
MAX_RECENT_EVENTS: Final[int] = 50


class ChallengeNotificationPayload(BaseModel):
    """Normalized payload exchanged over ``/ws/challenges`` and ``/api/challenges/notify``."""

    model_config = ConfigDict(extra="allow")

    event: str = Field(default=EVENT_SECURITY_CHALLENGE_DETECTED)
    challenge_type: str = Field(default="unknown")
    url: str = Field(default="")
    frame_url: str = Field(default="")
    message: str = Field(default=DEFAULT_CHALLENGE_MESSAGE)
    markers: list[str] = Field(default_factory=list)
    detected_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    resolved: bool = Field(default=False)


class ChallengeWebSocketManager:
    """Tracks active security challenge state and broadcasts alerts to dashboard clients."""

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []
        self.current_challenge: dict[str, Any] | None = None
        self.recent_events: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept a WebSocket client and push any currently active challenge immediately."""
        await websocket.accept()
        async with self._lock:
            if websocket not in self.active_connections:
                self.active_connections.append(websocket)
            snapshot = dict(self.current_challenge) if self.current_challenge else None

        if snapshot is not None:
            try:
                await websocket.send_json(snapshot)
            except Exception as exc:
                logger.debug("Failed sending initial active challenge snapshot: %s", exc)
                await self.disconnect(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a disconnected WebSocket client cleanly."""
        async with self._lock:
            self.active_connections = [
                conn for conn in self.active_connections if conn is not websocket
            ]

    async def broadcast_json(self, payload: dict[str, Any]) -> int:
        """Broadcast a JSON dictionary to all connected WebSocket dashboard clients."""
        async with self._lock:
            targets = list(self.active_connections)

        delivered = 0
        stale: list[WebSocket] = []
        for connection in targets:
            try:
                await connection.send_json(payload)
                delivered += 1
            except Exception as exc:
                logger.debug("Pruning disconnected /ws/challenges client: %s", exc)
                stale.append(connection)

        if stale:
            async with self._lock:
                self.active_connections = [
                    conn for conn in self.active_connections if conn not in stale
                ]
        return delivered

    async def handle_payload(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        """Validate, store, and broadcast a challenge detection or resolution payload."""
        model = ChallengeNotificationPayload(**raw_payload)
        normalized = model.model_dump()

        is_resolved = bool(
            normalized.get("resolved")
            or normalized.get("event") == EVENT_SECURITY_CHALLENGE_RESOLVED
        )
        normalized["resolved"] = is_resolved
        if is_resolved and normalized.get("event") == EVENT_SECURITY_CHALLENGE_DETECTED:
            normalized["event"] = EVENT_SECURITY_CHALLENGE_RESOLVED
        if not normalized.get("message"):
            normalized["message"] = (
                "Security challenge resolved; resuming automation."
                if is_resolved
                else DEFAULT_CHALLENGE_MESSAGE
            )

        async with self._lock:
            if is_resolved:
                self.current_challenge = None
            else:
                self.current_challenge = normalized
            self.recent_events.append(normalized)
            if len(self.recent_events) > MAX_RECENT_EVENTS:
                self.recent_events = self.recent_events[-MAX_RECENT_EVENTS:]

        clients_notified = await self.broadcast_json(normalized)
        return {
            "active": self.current_challenge is not None,
            "clients_notified": clients_notified,
            "challenge": normalized,
        }

    async def mark_resolved(
        self,
        message: str = "Security challenge resolved; resuming automation.",
    ) -> dict[str, Any]:
        """Clear active challenge status and broadcast resolution event."""
        async with self._lock:
            current = dict(self.current_challenge) if self.current_challenge else {}

        payload = {
            "event": EVENT_SECURITY_CHALLENGE_RESOLVED,
            "challenge_type": current.get("challenge_type", "manual"),
            "url": current.get("url", ""),
            "frame_url": current.get("frame_url", ""),
            "message": message,
            "markers": current.get("markers", []),
            "resolved": True,
        }
        return await self.handle_payload(payload)

    def get_status(self) -> dict[str, Any]:
        """Return current active challenge status and connected client count."""
        return {
            "active": self.current_challenge is not None,
            "challenge": dict(self.current_challenge) if self.current_challenge else None,
            "connected_clients": len(self.active_connections),
            "recent_events": list(self.recent_events[-10:]),
        }

    def clear(self) -> None:
        """Reset challenge state and recent events (useful in test isolation)."""
        self.current_challenge = None
        self.recent_events.clear()


default_challenge_manager = ChallengeWebSocketManager()


def get_challenge_manager(app: FastAPI | Any | None = None) -> ChallengeWebSocketManager:
    """Resolve the ``ChallengeWebSocketManager`` attached to a FastAPI app or fallback singleton."""
    if app is not None:
        state = getattr(app, "state", None)
        if state is not None and hasattr(state, "challenge_manager"):
            mgr = getattr(state, "challenge_manager")
            if isinstance(mgr, ChallengeWebSocketManager):
                return mgr
    return default_challenge_manager


router = APIRouter(tags=["challenges"])


@router.websocket("/ws/challenges")
async def challenge_notifications_ws(websocket: WebSocket) -> None:
    """WebSocket endpoint streaming and relaying security challenge notifications."""
    manager = get_challenge_manager(websocket.app)
    await manager.connect(websocket)
    try:
        while True:
            raw_text = await websocket.receive_text()
            stripped = raw_text.strip()
            if not stripped:
                continue
            if stripped.lower() == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                logger.debug("Ignoring non-JSON message on /ws/challenges.")
                continue

            if not isinstance(parsed, dict):
                continue
            if parsed.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            await manager.handle_payload(parsed)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket /ws/challenges session ended: %s", exc)
    finally:
        await manager.disconnect(websocket)


@router.get("/api/challenges/status")
async def get_challenge_status(request: Request) -> dict[str, Any]:
    """Return current active security challenge state for the HITL dashboard."""
    manager = get_challenge_manager(request.app)
    return manager.get_status()


@router.post("/api/challenges/notify")
async def post_challenge_notify(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """HTTP bridge to broadcast a security challenge notification to ``/ws/challenges`` clients."""
    manager = get_challenge_manager(request.app)
    result = await manager.handle_payload(payload)
    return {"status": "notified", **result}


@router.post("/api/challenges/resolve")
async def post_challenge_resolve(
    request: Request,
    payload: dict[str, Any] | None = Body(default=None),
) -> dict[str, Any]:
    """Mark the current security challenge resolved and notify dashboard listeners."""
    manager = get_challenge_manager(request.app)
    message = "Security challenge resolved; resuming automation."
    if isinstance(payload, dict) and payload.get("message"):
        message = str(payload["message"])
    result = await manager.mark_resolved(message=message)
    return {"status": "resolved", **result}


def include_challenge_routes(
    app: FastAPI,
    manager: ChallengeWebSocketManager | None = None,
) -> ChallengeWebSocketManager:
    """Mount challenge WebSocket and status endpoints onto a FastAPI application."""
    resolved_manager = manager or ChallengeWebSocketManager()
    app.state.challenge_manager = resolved_manager
    app.include_router(router)
    return resolved_manager


def create_challenge_app(
    manager: ChallengeWebSocketManager | None = None,
) -> FastAPI:
    """Create a standalone FastAPI application exposing only the challenge WebSocket routes."""
    challenge_app = FastAPI(
        title="JobApplier Security Challenge Notification Service",
        version="0.1.0",
    )
    include_challenge_routes(challenge_app, manager=manager)
    return challenge_app


__all__ = [
    "DEFAULT_CHALLENGE_MESSAGE",
    "EVENT_SECURITY_CHALLENGE_DETECTED",
    "EVENT_SECURITY_CHALLENGE_RESOLVED",
    "ChallengeNotificationPayload",
    "ChallengeWebSocketManager",
    "create_challenge_app",
    "default_challenge_manager",
    "get_challenge_manager",
    "include_challenge_routes",
    "router",
]
