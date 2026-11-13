"""Zero-token public ATS API feed scanners for JobApplier-Core.

Discovers open job postings directly from public Greenhouse and Lever JSON APIs
without launching a browser or consuming LLM tokens, applies configurable age and
role keyword filters, deduplicates entries, and persists newly discovered roles
into the local intake queue JSON file.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import urllib.parse
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Literal

import requests
import yaml
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

GREENHOUSE_BOARD_API_TEMPLATE: Final[str] = (
    "https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
)
LEVER_POSTINGS_API_TEMPLATE: Final[str] = (
    "https://api.lever.co/v0/postings/{slug}?mode=json"
)
DEFAULT_TIMEOUT_SECONDS: Final[float] = 15.0
DEFAULT_CONFIG_PATH: Final[Path] = Path("config") / "companies.yml"
DEFAULT_HISTORY_DIR_NAME: Final[str] = "history"
DEFAULT_QUEUE_FILENAME: Final[str] = "intake_queue.json"
DEFAULT_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (compatible; JobApplier-Core/1.0; +https://localhost)"
)

_HTML_TAG_PATTERN = re.compile(r"</?[a-zA-Z][^>]*>")
_ESCAPED_HTML_TAG_PATTERN = re.compile(r"&lt;/?[a-zA-Z][^&]*&gt;")


def resolve_intake_queue_path(queue_path: Path | str | None = None) -> Path:
    """Resolve the intake queue JSON path safely via HISTORY_PATH."""
    if queue_path is not None:
        return Path(queue_path)
    history_root = os.getenv("HISTORY_PATH", DEFAULT_HISTORY_DIR_NAME)
    return Path(history_root) / DEFAULT_QUEUE_FILENAME


def _normalize_keyword_list(value: Any) -> list[str]:
    """Normalize a keyword list or string into stripped, non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_items = [str(item).strip() for item in value if item is not None]
    else:
        return []
    return [item for item in raw_items if item]


class RoleFilters(BaseModel):
    """Role keyword and freshness filtering configuration."""

    model_config = ConfigDict(extra="ignore")

    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    max_age_days: int | None = None

    @field_validator("include_keywords", "exclude_keywords", mode="before")
    @classmethod
    def _validate_keywords(cls, value: Any) -> list[str]:
        return _normalize_keyword_list(value)

    @field_validator("max_age_days", mode="before")
    @classmethod
    def _validate_max_age(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        parsed = int(value)
        return max(0, parsed)


class CompanyFeedConfig(BaseModel):
    """Configuration for a single company public ATS feed."""

    model_config = ConfigDict(extra="ignore")

    name: str
    portal: Literal["greenhouse", "lever"]
    board_token: str
    enabled: bool = True
    role_filters: RoleFilters = Field(default_factory=RoleFilters)

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases_and_filters(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if not payload.get("board_token"):
            for alias in ("slug", "company_slug", "token"):
                if payload.get(alias):
                    payload["board_token"] = payload[alias]
                    break
        if "portal" in payload and isinstance(payload["portal"], str):
            payload["portal"] = payload["portal"].strip().lower()

        raw_filters = payload.get("role_filters")
        merged_filters: dict[str, Any] = dict(raw_filters) if isinstance(raw_filters, dict) else {}
        for key in ("include_keywords", "exclude_keywords", "max_age_days"):
            if key in payload and key not in merged_filters:
                merged_filters[key] = payload[key]
        payload["role_filters"] = merged_filters
        return payload

    @field_validator("name", "board_token", mode="before")
    @classmethod
    def _validate_non_empty(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("Company name and board_token must be non-empty.")
        return text


class DiscoveredJob(BaseModel):
    """Normalized job posting discovered from a public ATS API feed."""

    model_config = ConfigDict(extra="allow")

    id: str
    company: str
    board_token: str
    portal: str
    title: str
    location: str = ""
    url: str
    updated_at: str = ""
    description_text: str = ""
    fit_score: int | None = None
    hard_blocked: bool = False
    blocker_reasons: list[str] = Field(default_factory=list)
    evaluation_summary: str = ""
    status: str = "discovered"


class FeedScanResult(BaseModel):
    """Summary of an ATS feed scanning run."""

    updated_at: str
    companies_scanned: int = 0
    companies_skipped: int = 0
    companies_failed: int = 0
    fetched_count: int = 0
    filtered_count: int = 0
    duplicates_skipped: int = 0
    added_count: int = 0
    total_in_queue: int = 0
    new_jobs: list[DiscoveredJob] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def _html_to_text(raw: str) -> str:
    """Convert raw or entity-escaped HTML markup into clean plain text."""
    text = raw.strip()
    if not text:
        return ""

    # Greenhouse returns HTML-entity-encoded HTML (&lt;p&gt;...&lt;/p&gt;).
    if _ESCAPED_HTML_TAG_PATTERN.search(text):
        text = html.unescape(text)

    if _HTML_TAG_PATTERN.search(text):
        soup = BeautifulSoup(text, "html.parser")
        for noisy in soup(["script", "style"]):
            noisy.decompose()
        extracted = soup.get_text(separator="\n")
    else:
        extracted = html.unescape(text)

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in extracted.splitlines()]
    collapsed: list[str] = []
    prev_blank = False
    for line in lines:
        if not line:
            if collapsed and not prev_blank:
                collapsed.append("")
            prev_blank = True
        else:
            collapsed.append(line)
            prev_blank = False
    return "\n".join(collapsed).strip()


def _extract_text_from_value(value: Any) -> str:
    """Recursively extract clean description text from strings, dicts, or lists."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _html_to_text(value)
    if isinstance(value, dict):
        # Prefer explicit plain-text fields before HTML/body fields.
        for key in (
            "text",
            "plain",
            "descriptionPlain",
            "description_text",
            "body",
            "html",
            "content",
            "description",
            "rendered",
            "markup",
            "raw",
            "value",
        ):
            if key in value and value[key] is not None:
                extracted = _extract_text_from_value(value[key])
                if extracted:
                    return extracted
        ignored_meta_keys = {"format", "type", "encoding", "locale", "id", "lang"}
        parts = [
            _extract_text_from_value(v)
            for k, v in value.items()
            if str(k).lower() not in ignored_meta_keys
        ]
        return "\n\n".join(part for part in parts if part)
    if isinstance(value, list):
        parts = [_extract_text_from_value(item) for item in value]
        return "\n\n".join(part for part in parts if part)
    return _html_to_text(str(value))


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO 8601 string or Unix epoch timestamp (s or ms) into UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        epoch = float(value)
        if abs(epoch) > 1e11:
            epoch /= 1000.0
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        try:
            epoch = float(text)
            if abs(epoch) > 1e11:
                epoch /= 1000.0
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _normalize_timestamp(value: Any) -> str:
    """Convert raw timestamp value to canonical UTC ISO 8601 string, if parseable."""
    if value is None or value == "":
        return ""
    parsed = _parse_timestamp(value)
    if parsed is not None:
        return parsed.isoformat()
    return str(value).strip()


def _normalize_url(url: str) -> str:
    """Normalize a posting URL for reliable cross-run deduplication."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    query = parsed.query
    return urllib.parse.urlunparse((scheme, netloc, path, "", query, ""))


def _extract_greenhouse_location(item: dict[str, Any]) -> str:
    """Extract location string from a Greenhouse job dictionary."""
    loc = item.get("location")
    if isinstance(loc, dict):
        name = str(loc.get("name") or "").strip()
        if name:
            return name
    elif isinstance(loc, str) and loc.strip():
        return loc.strip()

    offices = item.get("offices")
    if isinstance(offices, list):
        office_names = [
            str(o.get("name") or o.get("location") or "").strip()
            for o in offices
            if isinstance(o, dict)
        ]
        office_names = [name for name in office_names if name]
        if office_names:
            return "; ".join(dict.fromkeys(office_names))
    return ""


def _extract_greenhouse_description(item: dict[str, Any]) -> str:
    """Extract description text gracefully when Greenhouse content is optional or nested."""
    for key in ("content", "description", "description_text", "body", "details"):
        if key in item and item[key] is not None:
            extracted = _extract_text_from_value(item[key])
            if extracted:
                return extracted
    return ""


def parse_greenhouse_jobs(
    payload: dict[str, Any],
    company_name: str = "",
    board_token: str = "",
) -> list[DiscoveredJob]:
    """Parse Greenhouse public job board JSON payload into DiscoveredJob objects."""
    if isinstance(payload, dict):
        raw_jobs = payload.get("jobs", [])
    elif isinstance(payload, list):
        raw_jobs = payload
    else:
        return []

    if not isinstance(raw_jobs, list):
        return []

    resolved_token = str(board_token or "").strip()
    resolved_company = str(company_name or resolved_token).strip()
    jobs: list[DiscoveredJob] = []

    for item in raw_jobs:
        if not isinstance(item, dict):
            continue

        raw_id = item.get("id")
        if raw_id is None:
            raw_id = item.get("internal_job_id")
        job_id = str(raw_id).strip() if raw_id is not None else ""
        title = str(item.get("title") or "").strip()
        url = str(
            item.get("absolute_url")
            or item.get("url")
            or item.get("hosted_url")
            or ""
        ).strip()

        if not url and job_id and resolved_token:
            url = f"https://boards.greenhouse.io/{resolved_token}/jobs/{job_id}"
        if not job_id and url:
            job_id = url.rstrip("/").rsplit("/", 1)[-1]
        if not job_id or not title or not url:
            continue

        updated_raw = (
            item.get("updated_at")
            or item.get("first_published")
            or item.get("created_at")
            or ""
        )
        item_company = str(
            resolved_company or item.get("company_name") or resolved_token
        ).strip()

        jobs.append(
            DiscoveredJob(
                id=job_id,
                company=item_company,
                board_token=resolved_token,
                portal="greenhouse",
                title=title,
                location=_extract_greenhouse_location(item),
                url=url,
                updated_at=_normalize_timestamp(updated_raw),
                description_text=_extract_greenhouse_description(item),
            )
        )

    return jobs


def _extract_lever_location(item: dict[str, Any]) -> str:
    """Extract location string from a Lever job posting dictionary."""
    categories = item.get("categories")
    if isinstance(categories, dict):
        loc = str(categories.get("location") or "").strip()
        if loc:
            return loc
        all_locs = categories.get("allLocations")
        if isinstance(all_locs, list):
            cleaned = [str(entry).strip() for entry in all_locs if str(entry or "").strip()]
            if cleaned:
                return "; ".join(dict.fromkeys(cleaned))

    top_loc = item.get("location")
    if isinstance(top_loc, dict):
        name = str(top_loc.get("name") or "").strip()
        if name:
            return name
    elif isinstance(top_loc, str) and top_loc.strip():
        return top_loc.strip()

    workplace = str(item.get("workplaceType") or "").strip()
    if workplace and workplace.lower() != "unspecified":
        return workplace.capitalize()
    return ""


def _extract_lever_description(item: dict[str, Any]) -> str:
    """Combine Lever opening, description, structured lists, and additional notes."""
    sections: list[str] = []

    opening = _extract_text_from_value(item.get("openingPlain") or item.get("opening"))
    if opening:
        sections.append(opening)

    main_desc = _extract_text_from_value(
        item.get("descriptionPlain")
        or item.get("description")
        or item.get("content")
    )
    if main_desc:
        sections.append(main_desc)

    lists = item.get("lists")
    if isinstance(lists, list):
        for block in lists:
            if not isinstance(block, dict):
                continue
            heading = str(block.get("text") or "").strip()
            body = _extract_text_from_value(block.get("content"))
            if heading and body:
                sections.append(f"{heading}\n{body}")
            elif body:
                sections.append(body)
            elif heading:
                sections.append(heading)

    additional = _extract_text_from_value(
        item.get("additionalPlain") or item.get("additional")
    )
    if additional:
        sections.append(additional)

    return "\n\n".join(sections).strip()


def parse_lever_jobs(
    payload: list[dict[str, Any]],
    company_name: str = "",
    slug: str = "",
) -> list[DiscoveredJob]:
    """Parse Lever public postings JSON payload into DiscoveredJob objects."""
    if isinstance(payload, list):
        raw_jobs = payload
    elif isinstance(payload, dict):
        raw_jobs = (
            payload.get("postings")
            or payload.get("data")
            or payload.get("jobs")
            or []
        )
    else:
        return []

    if not isinstance(raw_jobs, list):
        return []

    resolved_slug = str(slug or "").strip()
    resolved_company = str(company_name or resolved_slug).strip()
    jobs: list[DiscoveredJob] = []

    for item in raw_jobs:
        if not isinstance(item, dict):
            continue

        job_id = str(item.get("id") or "").strip()
        title = str(item.get("text") or item.get("title") or "").strip()
        url = str(
            item.get("hostedUrl")
            or item.get("url")
            or item.get("applyUrl")
            or ""
        ).strip()

        if not url and job_id and resolved_slug:
            url = f"https://jobs.lever.co/{resolved_slug}/{job_id}"
        if not job_id and url:
            job_id = url.rstrip("/").rsplit("/", 1)[-1]
        if not job_id or not title or not url:
            continue

        updated_raw = (
            item.get("updatedAt")
            or item.get("updated_at")
            or item.get("createdAt")
            or item.get("created_at")
            or ""
        )
        item_company = str(
            resolved_company or item.get("company") or resolved_slug
        ).strip()

        jobs.append(
            DiscoveredJob(
                id=job_id,
                company=item_company,
                board_token=resolved_slug,
                portal="lever",
                title=title,
                location=_extract_lever_location(item),
                url=url,
                updated_at=_normalize_timestamp(updated_raw),
                description_text=_extract_lever_description(item),
            )
        )

    return jobs


class ATSFeedScanner:
    """Lightweight, zero-browser ATS API scanner and intake queue manager."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        queue_path: Path | str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.config_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
        self._queue_path: Path | None = Path(queue_path) if queue_path is not None else None
        self.timeout = float(timeout)
        self.session = session
        self.user_agent = user_agent
        self.last_result: FeedScanResult | None = None

    @property
    def queue_path(self) -> Path:
        """Return explicit queue path or resolve dynamically via HISTORY_PATH."""
        return resolve_intake_queue_path(self._queue_path)

    @queue_path.setter
    def queue_path(self, value: Path | str | None) -> None:
        self._queue_path = Path(value) if value is not None else None

    def load_companies(
        self,
        config_path: Path | str | None = None,
        *,
        enabled_only: bool = False,
    ) -> list[CompanyFeedConfig]:
        """Load target company ATS feed configs from YAML."""
        target = Path(config_path) if config_path is not None else self.config_path
        if not target.exists():
            raise FileNotFoundError(f"Companies config file not found: {target}")

        raw_text = target.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
        if data is None:
            return []

        defaults: dict[str, Any] = {}
        if isinstance(data, dict):
            if isinstance(data.get("defaults"), dict):
                defaults = dict(data["defaults"])
            raw_companies = data.get("companies", [])
        elif isinstance(data, list):
            raw_companies = data
        else:
            raise ValueError(f"Invalid companies YAML structure in {target}")

        if not isinstance(raw_companies, list):
            raise ValueError(f"'companies' entry must be a list in {target}")

        configs: list[CompanyFeedConfig] = []
        for entry in raw_companies:
            if not isinstance(entry, dict):
                continue
            merged_entry = dict(entry)
            raw_filters = (
                dict(merged_entry.get("role_filters"))
                if isinstance(merged_entry.get("role_filters"), dict)
                else {}
            )
            for key in ("max_age_days", "include_keywords", "exclude_keywords"):
                if key in defaults and key not in raw_filters and key not in merged_entry:
                    raw_filters[key] = defaults[key]
            if raw_filters:
                merged_entry["role_filters"] = raw_filters

            config = CompanyFeedConfig(**merged_entry)
            if enabled_only and not config.enabled:
                continue
            configs.append(config)

        return configs

    def _http_get_json(self, url: str) -> Any:
        """Perform an HTTP GET request and parse JSON response."""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        requester = self.session if self.session is not None else requests
        response = requester.get(url, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def fetch_greenhouse_jobs(
        self,
        board_token: str,
        company_name: str = "",
    ) -> list[DiscoveredJob]:
        """Fetch and parse open postings from Greenhouse's public job board API."""
        token = board_token.strip()
        url = GREENHOUSE_BOARD_API_TEMPLATE.format(board_token=urllib.parse.quote(token))
        payload = self._http_get_json(url)
        return parse_greenhouse_jobs(
            payload=payload,
            company_name=company_name or token,
            board_token=token,
        )

    def fetch_lever_jobs(
        self,
        slug: str,
        company_name: str = "",
    ) -> list[DiscoveredJob]:
        """Fetch and parse open postings from Lever's public postings API."""
        cleaned_slug = slug.strip()
        url = LEVER_POSTINGS_API_TEMPLATE.format(slug=urllib.parse.quote(cleaned_slug))
        payload = self._http_get_json(url)
        return parse_lever_jobs(
            payload=payload,
            company_name=company_name or cleaned_slug,
            slug=cleaned_slug,
        )

    def fetch_company_jobs(self, company: CompanyFeedConfig) -> list[DiscoveredJob]:
        """Fetch jobs for a configured company according to its ATS portal."""
        if company.portal == "greenhouse":
            return self.fetch_greenhouse_jobs(
                board_token=company.board_token,
                company_name=company.name,
            )
        if company.portal == "lever":
            return self.fetch_lever_jobs(
                slug=company.board_token,
                company_name=company.name,
            )
        raise ValueError(f"Unsupported ATS portal: {company.portal}")

    @staticmethod
    def filter_jobs(
        jobs: Sequence[DiscoveredJob],
        role_filters: RoleFilters | None = None,
        *,
        now: datetime | None = None,
    ) -> list[DiscoveredJob]:
        """Filter out stale postings and apply include/exclude title keyword filters."""
        if role_filters is None:
            return list(jobs)

        reference_now = now or datetime.now(timezone.utc)
        if reference_now.tzinfo is None:
            reference_now = reference_now.replace(tzinfo=timezone.utc)
        else:
            reference_now = reference_now.astimezone(timezone.utc)

        include_terms = [kw.lower() for kw in role_filters.include_keywords if kw.strip()]
        exclude_terms = [kw.lower() for kw in role_filters.exclude_keywords if kw.strip()]
        max_age_days = role_filters.max_age_days
        cutoff = (
            reference_now - timedelta(days=max_age_days)
            if max_age_days is not None
            else None
        )

        kept: list[DiscoveredJob] = []
        for job in jobs:
            if cutoff is not None and job.updated_at:
                posted_dt = _parse_timestamp(job.updated_at)
                if posted_dt is not None and posted_dt < cutoff:
                    continue

            title_lower = job.title.lower()
            if include_terms and not any(term in title_lower for term in include_terms):
                continue
            if exclude_terms and any(term in title_lower for term in exclude_terms):
                continue

            kept.append(job)

        return kept

    @staticmethod
    def deduplicate_jobs(jobs: Sequence[DiscoveredJob]) -> list[DiscoveredJob]:
        """Deduplicate jobs by (portal, job_id) and normalized URL while preserving order."""
        seen_keys: set[tuple[str, str]] = set()
        seen_urls: set[str] = set()
        unique: list[DiscoveredJob] = []

        for job in jobs:
            key = (job.portal.strip().lower(), str(job.id).strip())
            norm_url = _normalize_url(job.url)
            if (key[0] and key[1] and key in seen_keys) or (norm_url and norm_url in seen_urls):
                continue
            if key[0] and key[1]:
                seen_keys.add(key)
            if norm_url:
                seen_urls.add(norm_url)
            unique.append(job)

        return unique

    def load_queue(self, queue_path: Path | str | None = None) -> list[dict[str, Any]]:
        """Read existing job entries from intake_queue.json if present."""
        target = resolve_intake_queue_path(queue_path) if queue_path is not None else self.queue_path
        if not target.exists() or not target.is_file():
            return []

        try:
            raw_text = target.read_text(encoding="utf-8")
            if not raw_text.strip():
                return []
            payload = json.loads(raw_text)
        except Exception as exc:
            logger.warning("Failed to parse intake queue at %s: %s", target, exc)
            return []

        if isinstance(payload, dict):
            raw_jobs = payload.get("jobs", [])
        elif isinstance(payload, list):
            raw_jobs = payload
        else:
            return []

        return [dict(item) for item in raw_jobs if isinstance(item, dict)]

    def save_queue(
        self,
        jobs: Sequence[DiscoveredJob | dict[str, Any]],
        queue_path: Path | str | None = None,
        *,
        updated_at: str | None = None,
    ) -> Path:
        """Atomically persist the intake queue JSON payload."""
        target = resolve_intake_queue_path(queue_path) if queue_path is not None else self.queue_path
        target.parent.mkdir(parents=True, exist_ok=True)

        serialized_jobs: list[dict[str, Any]] = []
        for item in jobs:
            if isinstance(item, DiscoveredJob):
                serialized_jobs.append(item.model_dump())
            else:
                serialized_jobs.append(dict(item))

        payload = {
            "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
            "jobs": serialized_jobs,
        }
        serialized = json.dumps(payload, indent=2, ensure_ascii=False)

        tmp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            tmp_path.write_text(serialized, encoding="utf-8")
            tmp_path.replace(target)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise
        return target

    def merge_into_queue(
        self,
        discovered_jobs: Sequence[DiscoveredJob],
        *,
        save: bool = True,
        queue_path: Path | str | None = None,
        now: datetime | None = None,
    ) -> tuple[list[DiscoveredJob], list[dict[str, Any]], int]:
        """Merge newly discovered jobs into the existing intake queue, preserving scored items."""
        existing = self.load_queue(queue_path=queue_path)
        seen_keys: set[tuple[str, str]] = set()
        seen_urls: set[str] = set()

        for entry in existing:
            portal = str(entry.get("portal") or "").strip().lower()
            job_id = str(entry.get("id") or "").strip()
            norm_url = _normalize_url(str(entry.get("url") or ""))
            if portal and job_id:
                seen_keys.add((portal, job_id))
            if norm_url:
                seen_urls.add(norm_url)

        newly_added: list[DiscoveredJob] = []
        duplicates_skipped = 0

        for job in discovered_jobs:
            key = (job.portal.strip().lower(), str(job.id).strip())
            norm_url = _normalize_url(job.url)
            if (key[0] and key[1] and key in seen_keys) or (norm_url and norm_url in seen_urls):
                duplicates_skipped += 1
                continue
            if key[0] and key[1]:
                seen_keys.add(key)
            if norm_url:
                seen_urls.add(norm_url)
            newly_added.append(job)

        combined: list[dict[str, Any]] = list(existing)
        combined.extend(job.model_dump() for job in newly_added)

        if save:
            timestamp = (now or datetime.now(timezone.utc)).isoformat()
            self.save_queue(combined, queue_path=queue_path, updated_at=timestamp)

        return newly_added, combined, duplicates_skipped

    def scan_company(
        self,
        company: CompanyFeedConfig,
        *,
        now: datetime | None = None,
    ) -> list[DiscoveredJob]:
        """Fetch, filter, and deduplicate jobs for a single company feed."""
        raw_jobs = self.fetch_company_jobs(company)
        filtered = self.filter_jobs(raw_jobs, company.role_filters, now=now)
        return self.deduplicate_jobs(filtered)

    def scan(
        self,
        *,
        save: bool = True,
        now: datetime | None = None,
    ) -> list[DiscoveredJob]:
        """Scan all enabled companies, filter/deduplicate postings, and update intake_queue.json."""
        reference_now = now or datetime.now(timezone.utc)
        if reference_now.tzinfo is None:
            reference_now = reference_now.replace(tzinfo=timezone.utc)
        else:
            reference_now = reference_now.astimezone(timezone.utc)

        companies = self.load_companies()
        companies_scanned = 0
        companies_skipped = 0
        companies_failed = 0
        fetched_count = 0
        filtered_jobs: list[DiscoveredJob] = []
        errors: list[str] = []

        for company in companies:
            if not company.enabled:
                companies_skipped += 1
                continue

            companies_scanned += 1
            try:
                raw_jobs = self.fetch_company_jobs(company)
                fetched_count += len(raw_jobs)
                matched = self.filter_jobs(raw_jobs, company.role_filters, now=reference_now)
                filtered_jobs.extend(matched)
            except Exception as exc:
                companies_failed += 1
                msg = f"{company.name} ({company.portal}:{company.board_token}): {exc}"
                logger.warning("ATS feed fetch failed for %s", msg)
                errors.append(msg)

        deduped_batch = self.deduplicate_jobs(filtered_jobs)
        batch_duplicates = len(filtered_jobs) - len(deduped_batch)
        newly_added, combined, queue_duplicates = self.merge_into_queue(
            deduped_batch,
            save=save,
            now=reference_now,
        )

        self.last_result = FeedScanResult(
            updated_at=reference_now.isoformat(),
            companies_scanned=companies_scanned,
            companies_skipped=companies_skipped,
            companies_failed=companies_failed,
            fetched_count=fetched_count,
            filtered_count=len(filtered_jobs),
            duplicates_skipped=batch_duplicates + queue_duplicates,
            added_count=len(newly_added),
            total_in_queue=len(combined),
            new_jobs=newly_added,
            errors=errors,
        )
        return newly_added


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for running a zero-token ATS feed scan."""
    parser = argparse.ArgumentParser(
        description="Scan configured Greenhouse and Lever public ATS feeds."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to companies YAML config (defaults to config/companies.yml).",
    )
    parser.add_argument(
        "--queue",
        type=Path,
        default=None,
        help="Override output intake_queue.json path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and filter jobs without writing intake_queue.json.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    scanner = ATSFeedScanner(config_path=args.config, queue_path=args.queue)
    new_jobs = scanner.scan(save=not args.dry_run)
    summary = scanner.last_result
    print(
        json.dumps(
            {
                "added_count": len(new_jobs),
                "total_in_queue": summary.total_in_queue if summary else len(new_jobs),
                "companies_scanned": summary.companies_scanned if summary else 0,
                "companies_failed": summary.companies_failed if summary else 0,
            },
            indent=2,
        )
    )
    return 0


__all__ = [
    "GREENHOUSE_BOARD_API_TEMPLATE",
    "LEVER_POSTINGS_API_TEMPLATE",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_CONFIG_PATH",
    "RoleFilters",
    "CompanyFeedConfig",
    "DiscoveredJob",
    "FeedScanResult",
    "resolve_intake_queue_path",
    "parse_greenhouse_jobs",
    "parse_lever_jobs",
    "ATSFeedScanner",
]

if __name__ == "__main__":
    raise SystemExit(main())
