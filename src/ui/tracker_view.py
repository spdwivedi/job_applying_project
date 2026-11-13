"""JobApplier-Core Pipeline & Funnel Tracker View.

FastAPI endpoints and template view for application lifecycle tracking,
funnel metrics visualization, interview scheduling, and status updates.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from src.engine.pipeline_tracker import (
    VALID_STATUSES,
    ApplicationRecord,
    ApplicationStatus,
    InterviewEvent,
    PipelineTracker,
    get_tracker,
)

logger = logging.getLogger(__name__)

# Templates directory resolution
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Request schemas
class UpdateStatusRequest(BaseModel):
    """Payload for updating application status."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    record_id: str | None = None
    id: str | None = None
    status: str | None = None
    new_status: str | None = None
    notes: str | None = None

    def get_id(self) -> str:
        return (self.record_id or self.id or "").strip()

    def get_status(self) -> str:
        return (self.status or self.new_status or "").strip()


class AddApplicationRequest(BaseModel):
    """Payload for adding or updating an application record."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = None
    company: str
    title: str
    portal: str = ""
    role_category: str = "full_time"
    date_applied: str | None = None
    status: str = ApplicationStatus.DRAFTED.value
    notes: str = ""
    portal_url: str = ""
    interviews: list[InterviewEvent] = Field(default_factory=list)
    dossier_path: str = ""


class AddInterviewRequest(BaseModel):
    """Payload for scheduling or recording an interview round."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    record_id: str | None = None
    id: str | None = None
    date: str
    round_name: str | None = None
    round: str | None = None
    notes: str = ""

    def get_id(self) -> str:
        return (self.record_id or self.id or "").strip()

    def get_round(self) -> str:
        return (self.round_name or self.round or "").strip()


# Shared Handler Functions
async def handle_get_tracker_dashboard(
    request: Request,
    status: str | None = None,
    role_category: str | None = None,
) -> Response:
    """Renders the tracker dashboard HTML page."""
    tracker = get_tracker()
    records = tracker.list_all(status=status, role_category=role_category)
    metrics = tracker.get_metrics()
    return templates.TemplateResponse(
        request=request,
        name="tracker.html",
        context={
            "records": [r.to_dict() for r in records],
            "metrics": metrics,
            "statuses": VALID_STATUSES,
            "role_categories": ["full_time", "internships"],
        },
    )


async def handle_get_tracker_data(
    status: str | None = None,
    role_category: str | None = None,
) -> dict[str, Any]:
    """Returns JSON list of all applications and funnel metrics."""
    tracker = get_tracker()
    records = tracker.list_all(status=status, role_category=role_category)
    metrics = tracker.get_metrics()
    return {
        "records": [r.to_dict() for r in records],
        "metrics": metrics,
        "total": metrics.get("total", len(records)),
    }


async def handle_update_status(payload: UpdateStatusRequest) -> dict[str, Any]:
    """Updates status of a job application record."""
    record_id = payload.get_id()
    new_status = payload.get_status()

    if not record_id:
        raise HTTPException(status_code=400, detail="Missing application record id.")
    if not new_status:
        raise HTTPException(status_code=400, detail="Missing new status.")

    tracker = get_tracker()
    success = tracker.update_status(record_id, new_status, notes=payload.notes)
    if not success:
        existing = tracker.get(record_id)
        if not existing:
            raise HTTPException(
                status_code=404, detail=f"Application record '{record_id}' not found."
            )
        raise HTTPException(
            status_code=400, detail=f"Invalid application status: '{new_status}'."
        )

    updated = tracker.get(record_id)
    return {
        "status": "success",
        "record": updated.to_dict() if updated else None,
        "metrics": tracker.get_metrics(),
    }


async def handle_add_application(payload: AddApplicationRequest) -> dict[str, Any]:
    """Adds a new application record."""
    if not payload.company or not payload.company.strip():
        raise HTTPException(status_code=400, detail="Company name is required.")
    if not payload.title or not payload.title.strip():
        raise HTTPException(status_code=400, detail="Job title is required.")

    tracker = get_tracker()
    record_dict = payload.model_dump(exclude_none=True)
    saved = tracker.add_or_update(record_dict)
    return {
        "status": "success",
        "record": saved.to_dict(),
        "metrics": tracker.get_metrics(),
    }


async def handle_add_interview(payload: AddInterviewRequest) -> dict[str, Any]:
    """Records an interview round event."""
    record_id = payload.get_id()
    round_name = payload.get_round()

    if not record_id:
        raise HTTPException(status_code=400, detail="Missing application record id.")
    if not round_name:
        raise HTTPException(status_code=400, detail="Missing interview round name.")
    if not payload.date or not payload.date.strip():
        raise HTTPException(status_code=400, detail="Missing interview date.")

    tracker = get_tracker()
    success = tracker.add_interview(
        record_id=record_id,
        date=payload.date.strip(),
        round_name=round_name,
        notes=payload.notes or "",
    )
    if not success:
        raise HTTPException(
            status_code=404, detail=f"Application record '{record_id}' not found."
        )

    updated = tracker.get(record_id)
    return {
        "status": "success",
        "record": updated.to_dict() if updated else None,
        "metrics": tracker.get_metrics(),
    }


# Primary APIRouter with prefix="/tracker"
router = APIRouter(prefix="/tracker", tags=["tracker"])

# 1. Dashboard HTML rendering at /tracker and /tracker/
router.add_api_route("", handle_get_tracker_dashboard, methods=["GET"], response_class=HTMLResponse)
router.add_api_route("/", handle_get_tracker_dashboard, methods=["GET"], response_class=HTMLResponse)

# 2. JSON API endpoints on /tracker
router.add_api_route("/api", handle_get_tracker_data, methods=["GET"])
router.add_api_route("/api/update-status", handle_update_status, methods=["POST"])
router.add_api_route("/api/add", handle_add_application, methods=["POST"])
router.add_api_route("/api/add-interview", handle_add_interview, methods=["POST"])

# Aliases for /tracker/api/tracker paths
router.add_api_route("/api/tracker", handle_get_tracker_data, methods=["GET"])
router.add_api_route("/api/tracker/update-status", handle_update_status, methods=["POST"])
router.add_api_route("/api/tracker/add", handle_add_application, methods=["POST"])
router.add_api_route("/api/tracker/add-interview", handle_add_interview, methods=["POST"])

# Standalone API router for /api/tracker prefix
api_router = APIRouter(prefix="/api/tracker", tags=["tracker-api"])
api_router.add_api_route("", handle_get_tracker_data, methods=["GET"])
api_router.add_api_route("/", handle_get_tracker_data, methods=["GET"])
api_router.add_api_route("/update-status", handle_update_status, methods=["POST"])
api_router.add_api_route("/add", handle_add_application, methods=["POST"])
api_router.add_api_route("/add-interview", handle_add_interview, methods=["POST"])

# Also attach root-level /api/tracker routes directly to router.routes
# This ensures that ANY app doing `app.include_router(router)` gets both /tracker and /api/tracker
router.routes.append(APIRoute("/api/tracker", handle_get_tracker_data, methods=["GET"]))
router.routes.append(APIRoute("/api/tracker/", handle_get_tracker_data, methods=["GET"]))
router.routes.append(APIRoute("/api/tracker/update-status", handle_update_status, methods=["POST"]))
router.routes.append(APIRoute("/api/tracker/add", handle_add_application, methods=["POST"]))
router.routes.append(APIRoute("/api/tracker/add-interview", handle_add_interview, methods=["POST"]))


def include_tracker_routes(app: FastAPI) -> None:
    """Convenience helper to mount all tracker UI and API routes on a FastAPI instance."""
    app.include_router(router)
    app.include_router(api_router)


def create_tracker_app(custom_tracker: PipelineTracker | None = None) -> FastAPI:
    """Create a standalone FastAPI application for the pipeline tracker."""
    tracker_app = FastAPI(
        title="JobApplier Pipeline Tracker",
        description="Central Application Pipeline & Funnel Tracker",
        version="0.1.0",
    )
    if custom_tracker:
        tracker_app.state.tracker = custom_tracker

    include_tracker_routes(tracker_app)
    return tracker_app
