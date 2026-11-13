"""Central Application Pipeline & Funnel Tracker for JobApplier-Core.

Tracks job application lifecycle stages, historical submissions, status changes,
interview schedules, and conversion funnel metrics.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Security & Path Resolution: Default storage under local history folder
HIST_DIR_NAME: Final[str] = "history"
DEFAULT_TRACKER_PATH: Final[Path] = Path(
    os.getenv("TRACKER_FILE_PATH", str(Path(HIST_DIR_NAME) / "tracker.json"))
)


class ApplicationStatus(str, Enum):
    """Permitted job application lifecycle statuses."""

    DRAFTED = "Drafted"
    VERIFIED = "Verified"
    SUBMITTED = "Submitted"
    FOLLOW_UP_DUE = "Follow-up Due"
    INTERVIEWING = "Interviewing"
    OFFER = "Offer"
    REJECTED = "Rejected"
    ARCHIVED = "Archived"


ApplicationStatusType = Literal[
    "Drafted",
    "Verified",
    "Submitted",
    "Follow-up Due",
    "Interviewing",
    "Offer",
    "Rejected",
    "Archived",
]

VALID_STATUSES: Final[list[str]] = [s.value for s in ApplicationStatus]
VALID_STATUS_MAP: Final[dict[str, str]] = {s.value.lower(): s.value for s in ApplicationStatus}

SUPPORTED_ROLE_CATEGORIES: Final[tuple[str, str]] = ("full_time", "internships")


def normalize_role_category(category: str | None) -> str:
    """Normalize input role category to either full_time or internships."""
    if not category or not isinstance(category, str):
        return "full_time"
    cleaned = category.strip().lower().replace("-", "_").replace(" ", "_")
    if "intern" in cleaned:
        return "internships"
    return "full_time"


def normalize_status(status_str: str) -> str | None:
    """Normalize status string to canonical casing if valid, else None."""
    if not status_str or not isinstance(status_str, str):
        return None
    return VALID_STATUS_MAP.get(status_str.strip().lower())


class InterviewEvent(BaseModel):
    """Recorded interview round event."""

    date: str
    round: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class ApplicationRecord(BaseModel):
    """Central data model for a tracked job application."""

    id: str = Field(default_factory=lambda: secrets.token_hex(8))
    company: str
    title: str
    portal: str = ""
    role_category: str = "full_time"
    date_applied: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    status: str = ApplicationStatus.DRAFTED.value
    notes: str = ""
    portal_url: str = ""
    interviews: list[InterviewEvent] = Field(default_factory=list)
    dossier_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class FunnelMetrics(BaseModel):
    """Conversion funnel and volume metrics."""

    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    conversion_rates: dict[str, float] = Field(default_factory=dict)
    by_role_category: dict[str, int] = Field(default_factory=dict)
    upcoming_interviews: list[dict[str, Any]] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class PipelineTracker:
    """Thread-safe application pipeline tracker with JSON persistence."""

    def __init__(self, storage_path: Path | str | None = None) -> None:
        """Initialize tracker pointing to specified or default JSON storage."""
        if storage_path is not None:
            self.storage_path = Path(storage_path)
        else:
            env_override = os.getenv("TRACKER_FILE_PATH")
            if env_override:
                self.storage_path = Path(env_override)
            else:
                self.storage_path = Path(HIST_DIR_NAME) / "tracker.json"

        self._lock = threading.RLock()
        self._records: dict[str, ApplicationRecord] = {}
        self.load()

    def load(self) -> None:
        """Load records from local JSON storage."""
        with self._lock:
            self._records.clear()
            if not self.storage_path.exists() or not self.storage_path.is_file():
                return

            try:
                raw_text = self.storage_path.read_text(encoding="utf-8")
                if not raw_text.strip():
                    return
                data = json.loads(raw_text)
                items = data.get("records", []) if isinstance(data, dict) else data
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            try:
                                rec = ApplicationRecord(**item)
                                self._records[rec.id] = rec
                            except Exception as ex:
                                logger.warning("Skipping malformed application record: %s (%s)", item, ex)
            except Exception as err:
                logger.error("Failed to read application records from %s: %s", self.storage_path, err)

    def save(self) -> None:
        """Atomically persist records to local JSON storage."""
        with self._lock:
            try:
                self.storage_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "version": 1,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "records": [rec.model_dump() for rec in self._records.values()],
                }
                serialized = json.dumps(payload, indent=2, ensure_ascii=False)

                # Atomic save using temp file
                tmp_file = self.storage_path.with_suffix(".tmp")
                tmp_file.write_text(serialized, encoding="utf-8")
                tmp_file.replace(self.storage_path)
            except Exception as err:
                logger.error("Failed to save application records to %s: %s", self.storage_path, err)
                raise

    def add_or_update(self, record: ApplicationRecord | dict[str, Any]) -> ApplicationRecord:
        """Add a new application record or update an existing one by ID."""
        with self._lock:
            if isinstance(record, dict):
                rec_dict = dict(record)
                if not rec_dict.get("id"):
                    rec_dict["id"] = secrets.token_hex(8)
                rec = ApplicationRecord(**rec_dict)
            else:
                rec = record.model_copy(deep=True)
                if not rec.id:
                    rec.id = secrets.token_hex(8)

            # Normalize values
            rec.role_category = normalize_role_category(rec.role_category)
            canonical_status = normalize_status(rec.status)
            if canonical_status:
                rec.status = canonical_status

            self._records[rec.id] = rec
            self.save()
            return rec

    def get(self, record_id: str) -> ApplicationRecord | None:
        """Retrieve an application record by ID."""
        with self._lock:
            rec = self._records.get(record_id)
            return rec.model_copy(deep=True) if rec else None

    def update_status(self, record_id: str, new_status: str, notes: str | None = None) -> bool:
        """Update status and optional notes for a tracked application."""
        with self._lock:
            rec = self._records.get(record_id)
            if not rec:
                return False

            canonical_status = normalize_status(new_status)
            if not canonical_status:
                logger.warning("Invalid application status requested: %s", new_status)
                return False

            rec.status = canonical_status
            if notes and notes.strip():
                if rec.notes:
                    rec.notes = f"{rec.notes}\n{notes.strip()}"
                else:
                    rec.notes = notes.strip()

            self.save()
            return True

    def add_interview(self, record_id: str, date: str, round_name: str, notes: str = "") -> bool:
        """Record an interview round event for an existing application."""
        with self._lock:
            rec = self._records.get(record_id)
            if not rec:
                return False

            event = InterviewEvent(
                date=str(date).strip(),
                round=str(round_name).strip(),
                notes=str(notes or "").strip(),
            )
            rec.interviews.append(event)

            # Auto-advance status to Interviewing if currently at early stages
            if rec.status in (
                ApplicationStatus.DRAFTED.value,
                ApplicationStatus.VERIFIED.value,
                ApplicationStatus.SUBMITTED.value,
                ApplicationStatus.FOLLOW_UP_DUE.value,
            ):
                rec.status = ApplicationStatus.INTERVIEWING.value

            self.save()
            return True

    def list_all(
        self,
        status: str | None = None,
        role_category: str | None = None,
    ) -> list[ApplicationRecord]:
        """List all tracked applications, with optional status and category filtering."""
        with self._lock:
            records = list(self._records.values())

        if status:
            target_status = normalize_status(status)
            if target_status:
                records = [r for r in records if r.status == target_status]
            else:
                lowered = status.strip().lower()
                records = [r for r in records if r.status.lower() == lowered]

        if role_category:
            target_cat = normalize_role_category(role_category)
            records = [r for r in records if normalize_role_category(r.role_category) == target_cat]

        # Sort descending by date_applied, fallback to id
        records.sort(key=lambda r: (r.date_applied or "", r.id), reverse=True)
        return [r.model_copy(deep=True) for r in records]

    def get_metrics(self) -> dict[str, Any]:
        """Calculate funnel conversion metrics, status distribution, and upcoming interviews."""
        with self._lock:
            records = list(self._records.values())

        total = len(records)
        by_status: dict[str, int] = {s.value: 0 for s in ApplicationStatus}
        by_role_category: dict[str, int] = {"full_time": 0, "internships": 0}

        for r in records:
            if r.status in by_status:
                by_status[r.status] += 1
            else:
                by_status[r.status] = 1

            cat = normalize_role_category(r.role_category)
            by_role_category[cat] = by_role_category.get(cat, 0) + 1

        # Funnel stage calculations
        # Submitted or beyond includes: Submitted, Follow-up Due, Interviewing, Offer, Rejected, Archived
        submitted_or_beyond_count = (
            by_status.get(ApplicationStatus.SUBMITTED.value, 0)
            + by_status.get(ApplicationStatus.FOLLOW_UP_DUE.value, 0)
            + by_status.get(ApplicationStatus.INTERVIEWING.value, 0)
            + by_status.get(ApplicationStatus.OFFER.value, 0)
            + by_status.get(ApplicationStatus.REJECTED.value, 0)
            + by_status.get(ApplicationStatus.ARCHIVED.value, 0)
        )
        interview_or_beyond_count = (
            by_status.get(ApplicationStatus.INTERVIEWING.value, 0)
            + by_status.get(ApplicationStatus.OFFER.value, 0)
        )
        offer_count = by_status.get(ApplicationStatus.OFFER.value, 0)

        # Rate calculations (expressed as percentage 0.0 - 100.0)
        submission_rate_pct = (
            round((submitted_or_beyond_count / total) * 100.0, 2) if total > 0 else 0.0
        )
        interview_rate_pct = (
            round((interview_or_beyond_count / submitted_or_beyond_count) * 100.0, 2)
            if submitted_or_beyond_count > 0
            else 0.0
        )
        offer_rate_pct = (
            round((offer_count / interview_or_beyond_count) * 100.0, 2)
            if interview_or_beyond_count > 0
            else 0.0
        )
        overall_conversion_pct = (
            round((offer_count / total) * 100.0, 2) if total > 0 else 0.0
        )

        conversion_rates = {
            "submission_rate_pct": submission_rate_pct,
            "interview_rate_pct": interview_rate_pct,
            "offer_rate_pct": offer_rate_pct,
            "overall_conversion_rate_pct": overall_conversion_pct,
            # Decimal ratios as well
            "submission_rate": round(submission_rate_pct / 100.0, 4),
            "interview_rate": round(interview_rate_pct / 100.0, 4),
            "offer_rate": round(offer_rate_pct / 100.0, 4),
            "overall_conversion_rate": round(overall_conversion_pct / 100.0, 4),
        }

        # Collect upcoming or recorded interviews across applications
        upcoming_interviews: list[dict[str, Any]] = []
        for r in records:
            for ev in r.interviews:
                upcoming_interviews.append(
                    {
                        "record_id": r.id,
                        "company": r.company,
                        "title": r.title,
                        "date": ev.date,
                        "round": ev.round,
                        "notes": ev.notes,
                        "status": r.status,
                    }
                )

        # Sort interviews by date ascending
        upcoming_interviews.sort(key=lambda item: item["date"])

        metrics = FunnelMetrics(
            total=total,
            by_status=by_status,
            conversion_rates=conversion_rates,
            by_role_category=by_role_category,
            upcoming_interviews=upcoming_interviews,
        )
        return metrics.model_dump()


# Singleton default tracker instance
_default_tracker: PipelineTracker | None = None
_tracker_init_lock = threading.Lock()


def get_tracker(storage_path: Path | str | None = None) -> PipelineTracker:
    """Obtain singleton PipelineTracker or instance with customized storage."""
    global _default_tracker
    if storage_path is not None:
        return PipelineTracker(storage_path=storage_path)

    if _default_tracker is None:
        with _tracker_init_lock:
            if _default_tracker is None:
                _default_tracker = PipelineTracker()
    return _default_tracker


# Top-level functional interface
def add_or_update(record: ApplicationRecord | dict[str, Any]) -> ApplicationRecord:
    """Add or update an application record using the default tracker."""
    return get_tracker().add_or_update(record)


def get(record_id: str) -> ApplicationRecord | None:
    """Retrieve an application record by ID using the default tracker."""
    return get_tracker().get(record_id)


def update_status(record_id: str, new_status: str, notes: str | None = None) -> bool:
    """Update status for a tracked application using the default tracker."""
    return get_tracker().update_status(record_id, new_status, notes=notes)


def add_interview(record_id: str, date: str, round_name: str, notes: str = "") -> bool:
    """Record an interview event for an application using the default tracker."""
    return get_tracker().add_interview(record_id, date, round_name, notes=notes)


def list_all(
    status: str | None = None,
    role_category: str | None = None,
) -> list[ApplicationRecord]:
    """List all tracked applications using the default tracker."""
    return get_tracker().list_all(status=status, role_category=role_category)


def get_metrics() -> dict[str, Any]:
    """Calculate funnel metrics using the default tracker."""
    return get_tracker().get_metrics()
