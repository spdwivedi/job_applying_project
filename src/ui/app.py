"""JobApplier-Core Human-in-the-Loop (HITL) Verification Dashboard.

Provides a local FastAPI web interface for inspecting and editing form fields,
previewing generated resume PDFs, reviewing job metadata, and explicitly approving
or cancelling portal submissions with secure approval tokens.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# Dynamic or decoupled schema definitions compatible with JobProfiler and FormParser
try:
    from src.agents.job_profiler import JobProfileSchema
except (ImportError, ModuleNotFoundError):
    class JobProfileSchema(BaseModel):
        job_title: str = "Unknown Job"
        company_name: str = "Unknown Company"
        location: str = "Unknown Location"
        work_type: str = "onsite"
        role_category: Literal["full_time", "internships"] = "full_time"
        required_skills: list[str] = Field(default_factory=list)
        preferred_skills: list[str] = Field(default_factory=list)
        tech_stack: list[str] = Field(default_factory=list)
        extracted_screening_questions: list[str] = Field(default_factory=list)
        raw_markdown: str = ""

try:
    from src.engine.form_parser import FormField, FormSchema
except (ImportError, ModuleNotFoundError):
    class FormField(BaseModel):
        id: str
        name: str = ""
        label: str = ""
        type: str = "text"
        required: bool = False
        options: list[str] = Field(default_factory=list)
        value: str = ""

    class FormSchema(BaseModel):
        fields: list[FormField] = Field(default_factory=list)

        def get_field_by_id(self, field_id: str) -> FormField | None:
            for field in self.fields:
                if field.id == field_id:
                    return field
            return None

        def get_resume_upload_field(self) -> FormField | None:
            for field in self.fields:
                if field.type == "file":
                    return field
            return None

        def to_dict(self) -> dict[str, Any]:
            return self.model_dump()


class HITLReviewState:
    """In-memory session state holder for Human-In-The-Loop review."""

    def __init__(
        self,
        job_profile: JobProfileSchema | None = None,
        form_schema: FormSchema | None = None,
        answers: dict[str, str] | None = None,
        resume_pdf_path: Path | None = None,
        submission_status: str = "AWAITING HUMAN APPROVAL",
        approval_token: str | None = None,
        on_save_callback: Callable[[dict[str, str]], None] | None = None,
        on_approve_callback: Callable[[str], None] | None = None,
        on_cancel_callback: Callable[[], None] | None = None,
    ) -> None:
        self.job_profile: JobProfileSchema | None = job_profile
        self.form_schema: FormSchema | None = form_schema
        self.answers: dict[str, str] = dict(answers or {})
        self.resume_pdf_path: Path | None = Path(resume_pdf_path) if resume_pdf_path else None
        self.submission_status: str = submission_status
        self.approval_token: str = approval_token or secrets.token_urlsafe(24)
        self.on_save_callback: Callable[[dict[str, str]], None] | None = on_save_callback
        self.on_approve_callback: Callable[[str], None] | None = on_approve_callback
        self.on_cancel_callback: Callable[[], None] | None = on_cancel_callback
        self.scratch_files: list[Path] = []

    def set_job_profile(self, profile: JobProfileSchema) -> None:
        self.job_profile = profile

    def set_form_schema(self, schema: FormSchema) -> None:
        self.form_schema = schema

    def set_answers(self, answers: dict[str, str]) -> None:
        self.answers = dict(answers)

    def set_resume_pdf(self, path: Path | str | None) -> None:
        self.resume_pdf_path = Path(path) if path else None

    def register_scratch_file(self, path: Path | str) -> None:
        file_path = Path(path)
        if file_path not in self.scratch_files:
            self.scratch_files.append(file_path)

    def save_fields(self, new_answers: dict[str, str]) -> dict[str, str]:
        self.answers.update(new_answers)
        if self.form_schema and hasattr(self.form_schema, "fields"):
            for field in self.form_schema.fields:
                if field.id in new_answers:
                    field.value = new_answers[field.id]
        if self.on_save_callback:
            try:
                self.on_save_callback(self.answers)
            except Exception:
                pass
        return self.answers

    def approve(self, token: str) -> bool:
        """Approves the application submission if the token matches."""
        if not token or token.strip() != self.approval_token.strip():
            return False
        self.submission_status = "APPROVED"
        if self.on_approve_callback:
            try:
                self.on_approve_callback(self.approval_token)
            except Exception:
                pass
        return True

    def cancel(self) -> None:
        """Cancels session, resets status, and cleans up registered scratch files."""
        self.submission_status = "CANCELLED"
        for scratch_path in self.scratch_files:
            try:
                if scratch_path.exists() and scratch_path.is_file():
                    scratch_path.unlink()
            except Exception:
                pass
        self.scratch_files.clear()
        if self.on_cancel_callback:
            try:
                self.on_cancel_callback()
            except Exception:
                pass

    def reset(self) -> None:
        """Completely resets review state for a new application."""
        self.cancel()
        self.job_profile = None
        self.form_schema = None
        self.answers.clear()
        self.resume_pdf_path = None
        self.submission_status = "IDLE"
        self.approval_token = secrets.token_urlsafe(24)

    def to_dict(self) -> dict[str, Any]:
        """Serializes current state to a JSON-compatible dictionary."""
        job_data = None
        if self.job_profile:
            if hasattr(self.job_profile, "model_dump"):
                job_data = self.job_profile.model_dump()
            elif hasattr(self.job_profile, "dict"):
                job_data = self.job_profile.dict()

        form_data = None
        if self.form_schema:
            if hasattr(self.form_schema, "model_dump"):
                form_data = self.form_schema.model_dump()
            elif hasattr(self.form_schema, "dict"):
                form_data = self.form_schema.dict()

        return {
            "submission_status": self.submission_status,
            "approval_token": self.approval_token,
            "has_job_profile": self.job_profile is not None,
            "job_profile": job_data,
            "has_form_schema": self.form_schema is not None,
            "form_schema": form_data,
            "answers": self.answers,
            "has_resume": bool(self.resume_pdf_path and self.resume_pdf_path.exists()),
            "resume_pdf_path": str(self.resume_pdf_path) if self.resume_pdf_path else None,
        }


# Singleton review state instance
state = HITLReviewState()

# Template directory
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app(custom_state: HITLReviewState | None = None) -> FastAPI:
    """Creates and configures the FastAPI HITL dashboard application."""
    review_app = FastAPI(
        title="JobApplier HITL Dashboard",
        description="Local human-in-the-loop review interface for autonomous job applications",
        version="0.1.0",
    )

    review_app.state.review_state = custom_state or state

    @review_app.get("/", response_class=HTMLResponse)
    async def get_review_dashboard(request: Request) -> Response:
        """Renders the comprehensive HITL review dashboard."""
        current_state: HITLReviewState = review_app.state.review_state
        has_resume = bool(
            current_state.resume_pdf_path and current_state.resume_pdf_path.exists()
        )
        challenge_mgr = getattr(review_app.state, "challenge_manager", None)
        active_challenge = getattr(challenge_mgr, "current_challenge", None) if challenge_mgr else None
        return templates.TemplateResponse(
            request=request,
            name="review.html",
            context={
                "job_profile": current_state.job_profile,
                "form_schema": current_state.form_schema,
                "answers": current_state.answers,
                "has_resume": has_resume,
                "submission_status": current_state.submission_status,
                "approval_token": current_state.approval_token,
                "active_challenge": active_challenge,
            },
        )

    @review_app.post("/api/save-fields")
    async def post_save_fields(request: Request) -> dict[str, Any]:
        """Accepts updated field values and persists them in review state."""
        current_state: HITLReviewState = review_app.state.review_state
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        if isinstance(payload, dict):
            if "answers" in payload and isinstance(payload["answers"], dict):
                incoming_answers = payload["answers"]
            else:
                incoming_answers = payload
        else:
            incoming_answers = {}

        # Convert all incoming values to strings
        sanitized_answers = {str(k): str(v) for k, v in incoming_answers.items()}
        current_state.save_fields(sanitized_answers)
        return {"status": "saved", "answers": current_state.answers}

    @review_app.post("/api/approve-submit")
    async def post_approve_submit(request: Request) -> dict[str, Any]:
        """The ONLY permitted endpoint for human submission authorization.

        Requires a valid approval token matching the current session.
        """
        current_state: HITLReviewState = review_app.state.review_state
        token_to_verify: str | None = None

        try:
            payload = await request.json()
            if isinstance(payload, dict):
                token_to_verify = payload.get("token")
        except Exception:
            pass

        if not token_to_verify:
            token_to_verify = request.query_params.get("token")

        if not token_to_verify or not current_state.approve(token_to_verify):
            raise HTTPException(
                status_code=400,
                detail="Invalid or missing approval token. Submission rejected.",
            )

        return {"status": "approved", "token": current_state.approval_token}

    @review_app.post("/api/cancel")
    async def post_cancel_review() -> dict[str, Any]:
        """Cancels application review session and cleans up scratch files."""
        current_state: HITLReviewState = review_app.state.review_state
        current_state.cancel()
        return {"status": "cancelled"}

    @review_app.get("/api/resume-pdf")
    async def get_resume_pdf(download: bool = False) -> Response:
        """Serves the tailored resume PDF if available."""
        current_state: HITLReviewState = review_app.state.review_state
        if not current_state.resume_pdf_path or not current_state.resume_pdf_path.exists():
            raise HTTPException(
                status_code=404,
                detail="Tailored resume PDF not found or not yet generated.",
            )

        filename = current_state.resume_pdf_path.name or "tailored_resume.pdf"
        disposition = "attachment" if download else "inline"
        headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}

        return FileResponse(
            path=str(current_state.resume_pdf_path),
            media_type="application/pdf",
            filename=filename,
            headers=headers,
        )

    @review_app.get("/api/status")
    async def get_review_status() -> dict[str, Any]:
        """Returns current review state as JSON."""
        current_state: HITLReviewState = review_app.state.review_state
        return current_state.to_dict()

    try:
        from src.ui.tracker_view import include_tracker_routes
        include_tracker_routes(review_app)
    except (ImportError, ModuleNotFoundError):
        pass

    try:
        from src.ui.challenge_ws import include_challenge_routes
        include_challenge_routes(review_app)
    except (ImportError, ModuleNotFoundError):
        pass

    return review_app


app = create_app(state)


def configure_review_session(
    job_profile: JobProfileSchema | None = None,
    form_schema: FormSchema | None = None,
    answers: dict[str, str] | None = None,
    resume_pdf_path: Path | str | None = None,
    submission_status: str = "AWAITING HUMAN APPROVAL",
    approval_token: str | None = None,
    scratch_files: list[Path | str] | None = None,
    on_save: Callable[[dict[str, str]], None] | None = None,
    on_approve: Callable[[str], None] | None = None,
    on_cancel: Callable[[], None] | None = None,
) -> HITLReviewState:
    """Configures the default in-memory review state for an application."""
    state.job_profile = job_profile
    state.form_schema = form_schema
    state.answers = dict(answers or {})
    state.resume_pdf_path = Path(resume_pdf_path) if resume_pdf_path else None
    state.submission_status = submission_status
    state.approval_token = approval_token or secrets.token_urlsafe(24)
    state.on_save_callback = on_save
    state.on_approve_callback = on_approve
    state.on_cancel_callback = on_cancel
    state.scratch_files = [Path(p) for p in scratch_files] if scratch_files else []
    return state


def get_server_port() -> int:
    """Resolves server port from environment (UI_PORT or API_PORT), defaulting to 8501."""
    port_str = os.getenv("UI_PORT") or os.getenv("API_PORT") or "8501"
    try:
        return int(port_str)
    except ValueError:
        return 8501


def run(host: str = "127.0.0.1", port: int | None = None) -> None:
    """Launches the HITL dashboard server via uvicorn."""
    import uvicorn

    server_port = port if port is not None else get_server_port()
    uvicorn.run(app, host=host, port=server_port)


if __name__ == "__main__":
    run()
