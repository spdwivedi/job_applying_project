"""Intelligent Fit Scoring & Hard-Blocker Filter for JobApplier-Core.

Evaluates target job postings against the candidate's master profile to detect
non-negotiable hard blockers (visa sponsorship, citizenship, security clearance,
geographic work authorization, and strict minimum years of experience) and
produces a calibrated 1-5 fit score with deterministic offline scoring and
optional Gemini structured output enhancement.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from src.engine.llm import GeminiClient

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_FILENAME: Final[str] = "master_profile.yaml"
DEFAULT_QUEUE_FILENAME: Final[str] = "intake_queue.json"
DEFAULT_EXPERIENCE_TOLERANCE_YEARS: Final[float] = 1.0
STAFF_IMPLIED_MIN_YEARS: Final[float] = 7.0

# Clearance rank ordering for deterministic comparison
CLEARANCE_LEVELS: Final[dict[str, int]] = {
    "none": 0,
    "n/a": 0,
    "false": 0,
    "public trust": 1,
    "confidential": 1,
    "secret": 2,
    "dod secret": 2,
    "top secret": 3,
    "ts": 3,
    "ts/sci": 4,
    "ts-sci": 4,
    "top secret/sci": 4,
    "q clearance": 3,
    "l clearance": 2,
}

# Normalized country aliases mapped to canonical lowercase identifiers
COUNTRY_ALIASES: Final[dict[str, str]] = {
    "us": "united states",
    "u.s.": "united states",
    "u.s.a.": "united states",
    "usa": "united states",
    "united states": "united states",
    "united states of america": "united states",
    "uk": "united kingdom",
    "u.k.": "united kingdom",
    "united kingdom": "united kingdom",
    "great britain": "united kingdom",
    "england": "united kingdom",
    "ca": "canada",
    "canada": "canada",
    "de": "germany",
    "germany": "germany",
    "fr": "france",
    "france": "france",
    "in": "india",
    "india": "india",
    "au": "australia",
    "australia": "australia",
    "sg": "singapore",
    "singapore": "singapore",
    "ie": "ireland",
    "ireland": "ireland",
    "nl": "netherlands",
    "netherlands": "netherlands",
    "ch": "switzerland",
    "switzerland": "switzerland",
    "jp": "japan",
    "japan": "japan",
    "eu": "european union",
    "european union": "european union",
}

# Location-safe country aliases excluding 2-letter codes that collide with US states (CA, DE, IN)
LOCATION_COUNTRY_ALIASES: Final[dict[str, str]] = {
    k: v for k, v in COUNTRY_ALIASES.items() if len(k) > 2 or k in {"us", "uk", "eu"}
}

US_STATE_CODES: Final[frozenset[str]] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
})

# Canonical skill names mapped to alias tokens (case-insensitive unless flagged)
SKILL_SYNONYMS: Final[dict[str, tuple[str, ...]]] = {
    "Python": ("python", "python3"),
    "TypeScript": ("typescript",),
    "JavaScript": ("javascript", "ecmascript"),
    "Go": ("golang",),  # Case-sensitive "Go" handled separately to avoid English verb false positives
    "Rust": ("rust", "rustlang"),
    "Java": ("java",),
    "C++": ("c++", "cpp"),
    "C#": ("c#", "csharp"),
    ".NET": (".net", "dotnet", "asp.net"),
    "Ruby": ("ruby", "ruby on rails", "rails"),
    "Scala": ("scala",),
    "Kotlin": ("kotlin",),
    "Swift": ("swift", "swiftui"),
    "Objective-C": ("objective-c", "objc"),
    "PHP": ("php", "laravel"),
    "SQL": ("sql",),
    "Bash": ("bash", "shell scripting"),
    "FastAPI": ("fastapi",),
    "Django": ("django",),
    "Flask": ("flask",),
    "Spring Boot": ("spring boot", "spring framework"),
    "React": ("react", "reactjs", "react.js"),
    "Next.js": ("next.js", "nextjs"),
    "Angular": ("angular", "angularjs"),
    "Vue": ("vue", "vuejs", "vue.js"),
    "Node.js": ("node.js", "nodejs"),
    "Express": ("express.js", "expressjs"),
    "GraphQL": ("graphql",),
    "REST": ("rest api", "rest apis", "restful"),
    "gRPC": ("grpc",),
    "Pydantic": ("pydantic",),
    "Playwright": ("playwright",),
    "Selenium": ("selenium",),
    "Pytest": ("pytest",),
    "Jest": ("jest",),
    "PyTorch": ("pytorch",),
    "TensorFlow": ("tensorflow",),
    "Scikit-learn": ("scikit-learn", "sklearn"),
    "Pandas": ("pandas",),
    "NumPy": ("numpy",),
    "LLM": ("llm", "llms", "large language model", "large language models", "genai", "generative ai"),
    "RAG": ("rag", "retrieval-augmented generation", "retrieval augmented generation"),
    "LangChain": ("langchain",),
    "LangGraph": ("langgraph",),
    "Hugging Face": ("hugging face", "huggingface", "transformers"),
    "PostgreSQL": ("postgresql", "postgres"),
    "MySQL": ("mysql",),
    "MongoDB": ("mongodb", "mongo"),
    "SQLite": ("sqlite",),
    "Redis": ("redis", "redis streams"),
    "Elasticsearch": ("elasticsearch", "opensearch"),
    "DynamoDB": ("dynamodb",),
    "Snowflake": ("snowflake",),
    "BigQuery": ("bigquery",),
    "Spark": ("apache spark", "pyspark", "spark"),
    "Airflow": ("airflow", "apache airflow"),
    "dbt": ("dbt",),
    "Kafka": ("kafka", "apache kafka"),
    "RabbitMQ": ("rabbitmq",),
    "Celery": ("celery",),
    "Docker": ("docker", "containers", "containerization"),
    "Kubernetes": ("kubernetes", "k8s"),
    "Terraform": ("terraform", "iac"),
    "Ansible": ("ansible",),
    "AWS": ("aws", "amazon web services"),
    "GCP": ("gcp", "google cloud", "google cloud platform"),
    "Azure": ("azure", "microsoft azure"),
    "ECS": ("ecs", "aws ecs"),
    "S3": ("s3", "aws s3"),
    "RDS": ("rds", "aws rds"),
    "Lambda": ("aws lambda",),
    "GitHub Actions": ("github actions",),
    "CI/CD": ("ci/cd", "cicd", "continuous integration"),
    "Linux": ("linux", "unix"),
    "Git": ("git",),
    "Microservices": ("microservices", "microservice"),
    "Distributed Systems": ("distributed systems", "distributed architecture"),
    "Asyncio": ("asyncio", "asynchronous"),
    "WebSockets": ("websockets", "websocket"),
    "Tailwind CSS": ("tailwind", "tailwindcss"),
    "HTML": ("html", "html5"),
    "CSS": ("css", "css3"),
    "Xcode": ("xcode",),
    "UIKit": ("uikit",),
    "CoreData": ("coredata", "core data"),
    "CocoaPods": ("cocoapods",),
    "Oracle": ("oracle db", "oracle database"),
    "Hibernate": ("hibernate",),
    "Maven": ("maven",),
    "Gradle": ("gradle",),
    "FPGA": ("fpga",),
    "VHDL": ("vhdl",),
    "RTOS": ("rtos", "freertos"),
    "Embedded C": ("embedded c", "firmware"),
}

_MONTH_NAMES: Final[dict[str, int]] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# Precompile skill boundary matchers
_SKILL_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = []
for _canonical, _aliases in SKILL_SYNONYMS.items():
    for _alias in _aliases:
        _escaped = re.escape(_alias)
        _pat = re.compile(rf"(?<![A-Za-z0-9_]){_escaped}(?![A-Za-z0-9_])", re.IGNORECASE)
        _SKILL_PATTERNS.append((_canonical, _alias.lower(), _pat))

# Case-sensitive matcher for Go programming language
_GO_CASE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:(?<=[\s,/;:(])Go(?=[,/;:\s).])|\bGo\s+(?:programming|language|backend|services|microservices)\b)"
)

_NON_TECH_ROLE_KEYWORDS: Final[tuple[str, ...]] = (
    "account executive",
    "sales representative",
    "business development",
    "registered nurse",
    "nurse practitioner",
    "physician",
    "paralegal",
    "attorney",
    "recruiter",
    "talent acquisition",
    "hr specialist",
    "warehouse associate",
    "truck driver",
    "chef",
    "cashier",
)

_TECH_ROLE_KEYWORDS: Final[tuple[str, ...]] = (
    "software",
    "engineer",
    "developer",
    "ml",
    "ai",
    "machine learning",
    "systems",
    "backend",
    "back-end",
    "frontend",
    "front-end",
    "full-stack",
    "full stack",
    "fullstack",
    "platform",
    "infrastructure",
    "devops",
    "sre",
    "reliability",
    "data",
    "architect",
    "security",
    "cloud",
    "automation",
    "qa",
    "test",
    "python",
    "typescript",
)


class FitEvaluationResult(BaseModel):
    """Structured fit evaluation output for a candidate-job pair."""

    fit_score: int = Field(
        ...,
        ge=1,
        le=5,
        description="Overall fit score on a 1-5 scale (5=direct match, 3-4=strong overlap, 1-2=mismatch/blocked).",
    )
    hard_blocked: bool = Field(
        default=False,
        description="True if one or more non-negotiable hard blockers were triggered.",
    )
    blocker_reasons: list[str] = Field(
        default_factory=list,
        description="Human-readable explanations of triggered hard blockers.",
    )
    matched_skills: list[str] = Field(
        default_factory=list,
        description="Requested job skills confirmed in the candidate profile.",
    )
    missing_skills: list[str] = Field(
        default_factory=list,
        description="Requested job skills not found in the candidate profile.",
    )
    evaluation_summary: str = Field(
        default="",
        description="Concise rationale summarizing fit score, strengths, gaps, and blockers.",
    )
    recommended_action: Literal["skip", "review", "priority_apply"] = Field(
        ...,
        description="Triage recommendation: 'skip', 'review', or 'priority_apply'.",
    )

    @model_validator(mode="after")
    def _enforce_scoring_invariants(self) -> FitEvaluationResult:
        """Ensure hard-blocked jobs are always capped at <=2 and marked skip."""
        self.blocker_reasons = _dedupe_preserve_order(self.blocker_reasons)
        self.matched_skills = _dedupe_preserve_order(self.matched_skills)
        self.missing_skills = _dedupe_preserve_order(self.missing_skills)

        if self.hard_blocked or self.blocker_reasons:
            self.hard_blocked = True
            self.fit_score = min(max(1, self.fit_score), 2)
            self.recommended_action = "skip"
        elif self.fit_score <= 2:
            self.recommended_action = "skip"
        elif self.fit_score == 5:
            self.recommended_action = "priority_apply"

        return self


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """Return stripped non-empty strings deduplicated case-insensitively."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = str(item).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def resolve_default_profile_path() -> Path:
    """Resolve default master profile YAML path from VAULT_PATH env var."""
    vault_dir = Path(os.getenv("VAULT_PATH", "vault"))
    return vault_dir / DEFAULT_PROFILE_FILENAME


def resolve_default_queue_path() -> Path:
    """Resolve default intake queue JSON path from HISTORY_PATH env var."""
    history_dir = Path(os.getenv("HISTORY_PATH", "history"))
    return history_dir / DEFAULT_QUEUE_FILENAME


def _parse_year_month(
    raw_value: Any,
    *,
    is_end: bool = False,
    reference_dt: datetime | None = None,
) -> tuple[int, int] | None:
    """Parse date expressions (YYYY-MM, YYYY, Month YYYY, present) to (year, month)."""
    if raw_value is None:
        return None
    text = str(raw_value).strip().lower()
    if not text:
        return None

    now_dt = reference_dt or datetime.now(timezone.utc)
    if text in {"present", "current", "now", "ongoing", "to date", "today"}:
        return (now_dt.year, now_dt.month)

    ym_match = re.search(r"\b(19\d\d|20\d\d)[-/.](\d{1,2})\b", text)
    if ym_match:
        year = int(ym_match.group(1))
        month = max(1, min(12, int(ym_match.group(2))))
        return (year, month)

    month_year = re.search(r"\b([a-z]{3,9})\s+(19\d\d|20\d\d)\b", text)
    if month_year:
        month_num = _MONTH_NAMES.get(month_year.group(1))
        if month_num is not None:
            return (int(month_year.group(2)), month_num)

    year_only = re.search(r"\b(19\d\d|20\d\d)\b", text)
    if year_only:
        return (int(year_only.group(1)), 12 if is_end else 1)

    return None


def _estimate_experience_years(
    experience_entries: Any,
    summary_entries: Any,
    *,
    reference_dt: datetime | None = None,
) -> float:
    """Compute non-overlapping candidate experience duration in years."""
    intervals: list[tuple[int, int]] = []
    now_dt = reference_dt or datetime.now(timezone.utc)

    if isinstance(experience_entries, list):
        for entry in experience_entries:
            if not isinstance(entry, dict):
                continue
            start_ym = _parse_year_month(entry.get("start_date"), is_end=False, reference_dt=now_dt)
            if start_ym is None:
                continue
            end_ym = _parse_year_month(entry.get("end_date"), is_end=True, reference_dt=now_dt)
            if end_ym is None:
                end_ym = (now_dt.year, now_dt.month)

            start_idx = start_ym[0] * 12 + start_ym[1]
            end_idx = end_ym[0] * 12 + end_ym[1]
            if end_idx >= start_idx:
                intervals.append((start_idx, end_idx))

    if intervals:
        intervals.sort(key=lambda pair: (pair[0], pair[1]))
        merged: list[list[int]] = [[intervals[0][0], intervals[0][1]]]
        for start_idx, end_idx in intervals[1:]:
            if start_idx <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end_idx)
            else:
                merged.append([start_idx, end_idx])
        total_months = sum((end_i - start_i + 1) for start_i, end_i in merged)
        return round(total_months / 12.0, 1)

    if isinstance(summary_entries, list):
        summary_text = " ".join(str(item) for item in summary_entries)
        match = re.search(r"\b(\d+(?:\.\d+)?)\+?\s*(?:years?|yrs?)\s+of\s+experience\b", summary_text, re.I)
        if match:
            return round(float(match.group(1)), 1)

    return 0.0


def _extract_skills_from_text(text: str) -> list[str]:
    """Extract canonical technical skills from free-form text."""
    if not text:
        return []

    found: list[str] = []
    seen: set[str] = set()

    for canonical, _alias, pattern in _SKILL_PATTERNS:
        if canonical in seen:
            continue
        if pattern.search(text):
            seen.add(canonical)
            found.append(canonical)

    if "Go" not in seen and _GO_CASE_PATTERN.search(text):
        seen.add("Go")
        found.append("Go")

    return found


def _canonicalize_skill_token(raw_skill: str) -> str:
    """Map an arbitrary skill label to its canonical catalog name if known."""
    cleaned = raw_skill.strip()
    if not cleaned:
        return ""

    lowered = cleaned.lower()
    if lowered in {"go", "golang"}:
        return "Go"

    for canonical, alias, _pattern in _SKILL_PATTERNS:
        if lowered == canonical.lower() or lowered == alias:
            return canonical

    return cleaned


def _parse_clearance_level(clearance_value: Any) -> tuple[str, int]:
    """Normalize a security clearance label and numeric level (0..4)."""
    raw = str(clearance_value or "None").strip()
    lowered = raw.lower()
    if not lowered or lowered in {"none", "n/a", "na", "no", "false", "null"}:
        return ("None", 0)

    for label in ("ts/sci", "ts-sci", "top secret/sci", "top secret", "ts", "q clearance", "dod secret", "secret", "public trust", "confidential", "l clearance"):
        if label in lowered:
            return (raw, CLEARANCE_LEVELS[label])

    return (raw, 1)


def _normalize_country_name(raw_country: str) -> str:
    """Normalize country name or ISO code to canonical lowercase country name."""
    cleaned = raw_country.strip().lower()
    return COUNTRY_ALIASES.get(cleaned, cleaned)


def _is_placeholder(value: str) -> bool:
    """Return True if value is a template placeholder token."""
    upper = value.strip().upper()
    return not upper or "PLACEHOLDER" in upper


def _extract_candidate_work_auth(profile_data: dict[str, Any]) -> dict[str, Any]:
    """Normalize candidate work authorization attributes from profile data."""
    raw_auth = profile_data.get("work_authorization")
    auth = raw_auth if isinstance(raw_auth, dict) else {}

    sponsorship_required = bool(auth.get("sponsorship_required", False))
    future_sponsorship_required = bool(auth.get("future_sponsorship_required", False))

    status_raw = str(auth.get("status", "")).strip()
    status_lower = status_raw.lower()
    citizenship_raw = str(auth.get("citizenship", "")).strip()
    citizenship_lower = citizenship_raw.lower()
    combined_status = f"{status_lower} {citizenship_lower}".strip()

    explicit_citizen = auth.get("is_us_citizen")
    has_non_citizen_marker = any(
        marker in combined_status
        for marker in ("non-citizen", "not a citizen", "visa", "opt", "cpt", "h-1b", "h1b", "tn ", "ead", "work permit")
    )
    is_us_citizen = (
        bool(explicit_citizen)
        if explicit_citizen is not None
        else (
            not sponsorship_required
            and not future_sponsorship_required
            and not has_non_citizen_marker
            and any(
                marker in combined_status
                for marker in ("us citizen", "u.s. citizen", "united states citizen", "citizen")
            )
        )
    )

    is_permanent_resident = (
        not sponsorship_required
        and not future_sponsorship_required
        and any(
            marker in combined_status
            for marker in ("green card", "permanent resident", "lawful permanent", "lpr")
        )
    )

    raw_countries = auth.get("countries_authorized", [])
    countries_normalized: set[str] = set()
    if isinstance(raw_countries, list):
        for country in raw_countries:
            c_str = str(country).strip()
            if c_str and not _is_placeholder(c_str):
                countries_normalized.add(_normalize_country_name(c_str))

    clearance_label, clearance_level = _parse_clearance_level(auth.get("security_clearance", "None"))

    return {
        "status": status_raw,
        "sponsorship_required": sponsorship_required,
        "future_sponsorship_required": future_sponsorship_required,
        "countries_authorized": countries_normalized,
        "security_clearance": clearance_label,
        "clearance_level": clearance_level,
        "is_us_citizen": is_us_citizen,
        "is_permanent_resident": is_permanent_resident,
    }


class JobFitScorer:
    """Evaluates job postings for hard blockers and 1-5 technical fit."""

    def __init__(
        self,
        profile_path: Path | str | None = None,
        profile_data: dict[str, Any] | None = None,
        *,
        gemini_client: GeminiClient | None = None,
        experience_tolerance_years: float = DEFAULT_EXPERIENCE_TOLERANCE_YEARS,
    ) -> None:
        """Initialize the JobFitScorer with a candidate master profile.

        Args:
            profile_path: Optional path to candidate YAML profile. Defaults to
                ``Path(os.getenv("VAULT_PATH", "vault")) / "master_profile.yaml"``
                with fallback to its ``.example`` sibling.
            profile_data: Optional in-memory profile dict (bypasses disk lookup).
            gemini_client: Optional ``GeminiClient`` for semantic fit scoring.
            experience_tolerance_years: Allowed gap (in years) below a JD's stated
                minimum years requirement before triggering a hard blocker.
        """
        self.profile_path = (
            Path(profile_path) if profile_path is not None else resolve_default_profile_path()
        )
        self.gemini_client = gemini_client
        self.experience_tolerance_years = max(0.0, float(experience_tolerance_years))

        self._profile_data: dict[str, Any] = {}
        self.work_authorization: dict[str, Any] = {}
        self.estimated_years_experience: float = 0.0
        self.candidate_skills: list[str] = []
        self.candidate_skill_keys: set[str] = set()
        self.candidate_corpus: str = ""
        self.candidate_summary: str = ""

        if profile_data is not None:
            self.load_profile_data(profile_data)
        else:
            self.load_profile()

    def load_profile(self) -> None:
        """Load candidate YAML profile from disk with ``.example`` fallback."""
        target_path = self.profile_path
        if not target_path.is_file():
            example_path = target_path.with_name(target_path.name + ".example")
            if example_path.is_file():
                target_path = example_path

        loaded: dict[str, Any] = {}
        if target_path.is_file():
            try:
                raw_text = target_path.read_text(encoding="utf-8")
                parsed = yaml.safe_load(raw_text)
                if isinstance(parsed, dict):
                    loaded = parsed
            except Exception:
                logger.exception("Failed to load candidate profile YAML from '%s'.", target_path)
        else:
            logger.warning("Candidate profile file not found at '%s'.", target_path)

        self.load_profile_data(loaded)

    def load_profile_data(self, data: dict[str, Any]) -> None:
        """Load and index candidate attributes from an in-memory dictionary."""
        self._profile_data = data if isinstance(data, dict) else {}
        cv = self._profile_data.get("cv")
        cv_dict = cv if isinstance(cv, dict) else {}
        sections = cv_dict.get("sections")
        sections_dict = sections if isinstance(sections, dict) else {}

        self.work_authorization = _extract_candidate_work_auth(self._profile_data)

        exp_entries = sections_dict.get("experience", [])
        summary_entries = sections_dict.get("summary", [])
        self.estimated_years_experience = _estimate_experience_years(exp_entries, summary_entries)

        if isinstance(summary_entries, list):
            self.candidate_summary = " ".join(str(s).strip() for s in summary_entries if str(s).strip())
        elif isinstance(summary_entries, str):
            self.candidate_summary = summary_entries.strip()
        else:
            self.candidate_summary = ""

        corpus_parts: list[str] = []
        if self.candidate_summary:
            corpus_parts.append(self.candidate_summary)

        extracted_skills: list[str] = []
        seen_skill_keys: set[str] = set()

        def _register_skill(token: str) -> None:
            cleaned = token.strip()
            if not cleaned or _is_placeholder(cleaned):
                return
            canonical = _canonicalize_skill_token(cleaned)
            for candidate_key in (canonical.lower(), cleaned.lower()):
                if candidate_key and candidate_key not in seen_skill_keys:
                    seen_skill_keys.add(candidate_key)
                    if candidate_key == canonical.lower():
                        extracted_skills.append(canonical)

        # 1. Explicit skills section (including parenthetical items like AWS (ECS, S3, RDS))
        skills_section = sections_dict.get("skills", [])
        if isinstance(skills_section, list):
            for entry in skills_section:
                if not isinstance(entry, dict):
                    continue
                details = str(entry.get("details", ""))
                corpus_parts.append(details)
                # Extract parenthetical contents first, e.g. AWS (ECS, S3, RDS)
                for paren_match in re.finditer(r"([A-Za-z0-9_.+#-]+)\s*\(([^)]+)\)", details):
                    _register_skill(paren_match.group(1))
                    for sub in re.split(r"[,;/|]", paren_match.group(2)):
                        _register_skill(sub)
                stripped_parens = re.sub(r"\([^)]*\)", "", details)
                for raw_tok in re.split(r"[,;/|]", stripped_parens):
                    _register_skill(raw_tok)

        # 2. Experience, Projects, Education, Story Bank text
        for section_name in ("experience", "projects", "education"):
            entries = sections_dict.get(section_name, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                for key in ("position", "title", "name", "area", "degree"):
                    val = str(entry.get(key, "")).strip()
                    if val and not _is_placeholder(val):
                        corpus_parts.append(val)
                highlights = entry.get("highlights", [])
                if isinstance(highlights, list):
                    for hl in highlights:
                        hl_str = str(hl).strip()
                        if hl_str:
                            corpus_parts.append(hl_str)

        story_bank = self._profile_data.get("story_bank", [])
        if isinstance(story_bank, list):
            for story in story_bank:
                if not isinstance(story, dict):
                    continue
                for key in ("title", "situation", "task", "action", "result"):
                    val = str(story.get(key, "")).strip()
                    if val:
                        corpus_parts.append(val)
                for comp in story.get("competencies", []) or []:
                    comp_str = str(comp).replace("_", " ").strip()
                    if comp_str:
                        corpus_parts.append(comp_str)

        self.candidate_corpus = "\n".join(corpus_parts)

        # Supplement explicit skills with catalog skills discovered across profile corpus
        for discovered in _extract_skills_from_text(self.candidate_corpus):
            _register_skill(discovered)

        self.candidate_skills = extracted_skills
        self.candidate_skill_keys = seen_skill_keys

    def _normalize_job_input(
        self,
        job: Any,
        *,
        job_title: str = "",
        company: str = "",
        location: str = "",
        required_skills: list[str] | None = None,
        preferred_skills: list[str] | None = None,
    ) -> dict[str, Any]:
        """Coerce JobProfileSchema, dict, or raw text into a uniform job payload."""
        title = job_title.strip()
        comp = company.strip()
        loc = location.strip()
        req_skills = list(required_skills) if required_skills else []
        pref_skills = list(preferred_skills) if preferred_skills else []
        tech_stack: list[str] = []
        jd_text = ""

        if isinstance(job, str):
            jd_text = job.strip()
        elif isinstance(job, dict):
            title = title or str(job.get("job_title") or job.get("title") or "").strip()
            comp = comp or str(job.get("company_name") or job.get("company") or "").strip()
            loc = loc or str(job.get("location") or "").strip()
            jd_text = str(
                job.get("description_text")
                or job.get("description")
                or job.get("raw_markdown")
                or job.get("raw_text")
                or ""
            ).strip()
            if not req_skills and isinstance(job.get("required_skills"), list):
                req_skills = [str(s).strip() for s in job["required_skills"] if str(s).strip()]
            if not pref_skills and isinstance(job.get("preferred_skills"), list):
                pref_skills = [str(s).strip() for s in job["preferred_skills"] if str(s).strip()]
            if isinstance(job.get("tech_stack"), list):
                tech_stack = [str(s).strip() for s in job["tech_stack"] if str(s).strip()]
        else:
            # Duck-type JobProfileSchema or similar Pydantic models
            title = title or str(getattr(job, "job_title", "") or getattr(job, "title", "") or "").strip()
            comp = comp or str(getattr(job, "company_name", "") or getattr(job, "company", "") or "").strip()
            loc = loc or str(getattr(job, "location", "") or "").strip()
            jd_text = str(getattr(job, "raw_markdown", "") or getattr(job, "description_text", "") or "").strip()
            if not req_skills:
                req_skills = [str(s).strip() for s in (getattr(job, "required_skills", None) or []) if str(s).strip()]
            if not pref_skills:
                pref_skills = [str(s).strip() for s in (getattr(job, "preferred_skills", None) or []) if str(s).strip()]
            tech_stack = [str(s).strip() for s in (getattr(job, "tech_stack", None) or []) if str(s).strip()]

        if not title and jd_text:
            first_line = jd_text.splitlines()[0].strip().lstrip("#").strip()
            if first_line.lower().startswith("title:"):
                title = first_line.split(":", 1)[1].strip()

        return {
            "job_title": title,
            "company": comp,
            "location": loc,
            "required_skills": req_skills,
            "preferred_skills": pref_skills,
            "tech_stack": tech_stack,
            "jd_text": jd_text,
        }

    def _check_sponsorship_blocker(self, combined_text: str) -> str | None:
        """Detect visa sponsorship prohibitions conflicting with candidate auth."""
        needs_sponsorship = (
            self.work_authorization["sponsorship_required"]
            or self.work_authorization["future_sponsorship_required"]
        )
        if not needs_sponsorship:
            return None

        no_sponsorship_patterns = (
            r"\b(?:will\s+not|won['’]?t|does\s+not|do\s+not|cannot|can['’]?t|unable\s+to|not\s+able\s+to)\s+(?:provide\s+|offer\s+|support\s+)?(?:any\s+)?(?:visa\s+|work\s+)?sponsorship\b",
            r"\bno\s+(?:visa\s+|immigration\s+|h-?1b\s+)?sponsorship\b",
            r"\b(?:visa\s+)?sponsorship\b(?:\s+\w+){0,4}\s+(?:is\s+)?(?:not\s+available|not\s+offered|not\s+provided|not\s+supported|unavailable)\b",
            r"\bnot\s+eligible\s+for\s+(?:visa\s+)?sponsorship\b",
            r"\bwithout\s+(?:the\s+need\s+for\s+|needing\s+|requiring\s+)?(?:visa\s+)?sponsorship\b",
            r"\bmust\s+not\s+require\s+(?:visa\s+)?sponsorship\b",
            r"\bunable\s+to\s+sponsor\b",
            r"\bnot\s+sponsoring\b",
        )
        for pat in no_sponsorship_patterns:
            if re.search(pat, combined_text, re.IGNORECASE):
                return (
                    "Visa sponsorship blocker: JD states sponsorship is not provided, "
                    "while candidate profile requires current or future sponsorship."
                )
        return None

    def _check_citizenship_blocker(self, combined_text: str) -> str | None:
        """Detect citizenship or permanent residency requirements not met by candidate."""
        # Ignore EEO non-discrimination clauses mentioning citizenship
        sanitized = re.sub(
            r"(?:regardless\s+of|without\s+regard\s+to|based\s+on|status\s+or)\s+[^.\n]*?\bcitizenship\b",
            "",
            combined_text,
            flags=re.IGNORECASE,
        )

        is_us_citizen = self.work_authorization["is_us_citizen"]
        is_us_person = is_us_citizen or self.work_authorization["is_permanent_resident"]

        us_citizen_or_pr_patterns = (
            r"\bu\.?s\.?\s+citizens?\s+or\s+(?:lawful\s+)?permanent\s+residents?\b",
            r"\bu\.?s\.?\s+citizens?\s+or\s+green\s+card\b",
            r"\bgreen\s+card\s+(?:holder\s+)?or\s+u\.?s\.?\s+citizen\b",
            r"\bu\.?s\.?\s+persons?\s+only\b",
            r"\bmust\s+be\s+a\s+u\.?s\.?\s+person\b",
            r"\bitar\b",
        )
        for pat in us_citizen_or_pr_patterns:
            if re.search(pat, sanitized, re.IGNORECASE):
                if not is_us_person:
                    return (
                        "Citizenship/US Person blocker: JD requires U.S. Citizen or Permanent Resident status, "
                        "which is not confirmed in candidate profile."
                    )
                return None

        us_citizen_only_patterns = (
            r"\b(?:u\.?s\.?|united\s+states)\s+citizens?\s+only\b",
            r"\bmust\s+be\s+(?:a\s+)?(?:u\.?s\.?|united\s+states)\s+citizen\b",
            r"\b(?:u\.?s\.?|united\s+states)\s+citizenship\s+(?:is\s+)?required\b",
            r"\brequires?\s+(?:u\.?s\.?|united\s+states)\s+citizenship\b",
            r"\bonly\s+(?:u\.?s\.?|united\s+states)\s+citizens?\b",
        )
        for pat in us_citizen_only_patterns:
            if re.search(pat, sanitized, re.IGNORECASE):
                if not is_us_citizen:
                    return (
                        "Citizenship blocker: JD requires U.S. citizenship, "
                        "which is not confirmed in candidate profile."
                    )
                return None

        return None

    def _check_clearance_blocker(self, combined_text: str) -> str | None:
        """Detect active security clearance requirements exceeding candidate clearance."""
        # Strip explicit non-requirement phrases such as "no security clearance required"
        sanitized = re.sub(
            r"\b(?:no|not|without)\s+(?:active\s+)?(?:security\s+)?clearance\s+(?:is\s+)?required\b",
            "",
            combined_text,
            flags=re.IGNORECASE,
        )

        clearance_patterns: list[tuple[str, int, str]] = [
            (r"\b(?:active|current|existing|requires?|must\s+(?:hold|possess|have|maintain))\s+(?:an?\s+)?(?:active\s+)?(?:ts\s*/?\s*sci|ts-sci|top\s*secret\s*/\s*sci)\b", 4, "TS/SCI"),
            (r"\b(?:ts\s*/?\s*sci|ts-sci|top\s*secret/sci)\s+(?:security\s+)?clearance\s+(?:is\s+)?(?:required|mandatory|needed)\b", 4, "TS/SCI"),
            (r"\b(?:active|current|existing|requires?|must\s+(?:hold|possess|have|maintain))\s+(?:an?\s+)?(?:active\s+)?top\s*secret\s+(?:security\s+)?clearance\b", 3, "Top Secret"),
            (r"\btop\s*secret\s+(?:security\s+)?clearance\s+(?:is\s+)?(?:required|mandatory|needed)\b", 3, "Top Secret"),
            (r"\b(?:active|current|existing|requires?|must\s+(?:hold|possess|have|maintain))\s+(?:an?\s+)?(?:active\s+)?(?:dod\s+)?secret\s+(?:security\s+)?clearance\b", 2, "Secret"),
            (r"\bsecret\s+(?:security\s+)?clearance\s+(?:is\s+)?(?:required|mandatory|needed)\b", 2, "Secret"),
            (r"\b(?:active|current|existing|requires?|must\s+(?:hold|possess|have|maintain))\s+(?:an?\s+)?(?:active\s+)?(?:public\s*trust)\s+(?:security\s+)?clearance\b", 1, "Public Trust"),
            (r"\b(?:active|current|existing)\s+(?:government\s+|dod\s+|federal\s+|u\.?s\.?\s+){0,3}security\s+clearance\b", 2, "Active Security Clearance"),
            (r"\bsecurity\s+clearance\s+(?:is\s+)?(?:required|mandatory|needed)\b", 2, "Security Clearance"),
            (r"\bmust\s+(?:hold|possess|have|maintain)\s+(?:an?\s+)?(?:active\s+)?security\s+clearance\b", 2, "Security Clearance"),
            (r"\brequires?\s+(?:an?\s+)?(?:active\s+)?security\s+clearance\b", 2, "Security Clearance"),
        ]

        required_level = 0
        required_label = ""
        for pat, level, label in clearance_patterns:
            if re.search(pat, sanitized, re.IGNORECASE):
                if level > required_level:
                    required_level = level
                    required_label = label

        if required_level > 0:
            candidate_level = int(self.work_authorization["clearance_level"])
            candidate_label = self.work_authorization["security_clearance"]
            if candidate_level < required_level:
                return (
                    f"Security clearance blocker: JD requires '{required_label}', "
                    f"while candidate profile reports '{candidate_label}'."
                )

        # Eligibility-to-obtain clearance still blocks candidates needing sponsorship
        if re.search(
            r"\b(?:eligible\s+(?:to\s+obtain|for)|ability\s+to\s+obtain)\s+(?:a\s+)?(?:security\s+)?clearance\b",
            sanitized,
            re.IGNORECASE,
        ):
            if (
                self.work_authorization["sponsorship_required"]
                or self.work_authorization["future_sponsorship_required"]
            ):
                return (
                    "Security clearance eligibility blocker: JD requires clearance eligibility, "
                    "which conflicts with sponsorship requirement."
                )

        return None

    def _check_country_auth_blocker(self, combined_text: str, location: str) -> str | None:
        """Detect explicit geographic work authorization conflicts against countries_authorized."""
        authorized_countries: set[str] = self.work_authorization["countries_authorized"]
        if not authorized_countries:
            return None

        # 1. Explicit "authorized to work in <Country>" or "must reside in <Country>" patterns
        auth_country_patterns = (
            r"\b(?:legally\s+)?authorized\s+to\s+work\s+in\s+(?:the\s+)?(united\s+states|u\.?s\.?a?\.?|canada|united\s+kingdom|u\.?k\.?|germany|france|india|australia|singapore|ireland|netherlands|switzerland|japan|european\s+union|eu)\b",
            r"\bwork\s+authorization\s+(?:in|for)\s+(?:the\s+)?(united\s+states|u\.?s\.?a?\.?|canada|united\s+kingdom|u\.?k\.?|germany|france|india|australia|singapore|ireland|netherlands|switzerland|japan|european\s+union|eu)\b",
            r"\bmust\s+(?:reside|be\s+located|be\s+based)\s+in\s+(?:the\s+)?(united\s+states|u\.?s\.?a?\.?|canada|united\s+kingdom|u\.?k\.?|germany|france|india|australia|singapore|ireland|netherlands|switzerland|japan|european\s+union|eu)\b",
        )
        for pat in auth_country_patterns:
            for match in re.finditer(pat, combined_text, re.IGNORECASE):
                req_country = _normalize_country_name(match.group(1))
                if req_country and req_country not in authorized_countries:
                    return (
                        f"Country authorization blocker: JD requires authorization/residency in "
                        f"'{req_country.title()}', not listed in candidate countries_authorized."
                    )

        # 2. Explicit non-remote job location naming an unauthorized country
        loc_clean = location.strip()
        if loc_clean and not re.search(r"\b(?:anywhere|global)\b", loc_clean, re.I):
            explicit_country_found: str | None = None
            for alias, canonical_country in LOCATION_COUNTRY_ALIASES.items():
                if len(alias) <= 2:
                    matched = bool(re.search(rf"(?:^|[,\s(]){re.escape(alias.upper())}(?:$|[,\s)])", loc_clean))
                else:
                    matched = bool(re.search(rf"\b{re.escape(alias)}\b", loc_clean, re.I))
                if matched:
                    explicit_country_found = canonical_country
                    if canonical_country not in authorized_countries:
                        return (
                            f"Country authorization blocker: Job location '{loc_clean}' requires work "
                            f"authorization in '{canonical_country.title()}', not in candidate countries_authorized."
                        )

            if explicit_country_found is None:
                has_us_state = any(
                    re.search(rf",\s*{code}\b", loc_clean) for code in US_STATE_CODES
                )
                if has_us_state and "united states" not in authorized_countries:
                    return (
                        "Country authorization blocker: Job location is in the United States, "
                        "which is not in candidate countries_authorized."
                    )

        return None

    def _check_experience_blocker(
        self,
        jd_text: str,
        job_title: str,
        tolerance_years: float,
    ) -> tuple[str | None, float | None]:
        """Extract strict minimum years requirement and verify against candidate experience."""
        strict_years: list[float] = []
        preferred_line_markers = ("preferred", "nice to have", "bonus", "plus", "desired", "ideal")

        patterns = (
            re.compile(
                r"(?:minimum(?:\s+of)?|at\s+least|requires?|must\s+have)\s+(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?:\s+of)?(?:\s+[A-Za-z0-9/+.-]+){0,5}\s+experience",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(\d{1,2})\s*(?:\+|or\s+more)\s*(?:years?|yrs?)(?:\s+of)?(?:\s+[A-Za-z0-9/+.-]+){0,5}\s+(?:experience|industry|professional|software|engineering|development)",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*(?:years?|yrs?)(?:\s+of)?(?:\s+[A-Za-z0-9/+.-]+){0,5}\s+experience",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(\d{1,2})\s*\+\s*(?:years?|yrs?)\s+in\s+[A-Za-z]",
                re.IGNORECASE,
            ),
        )

        for raw_line in jd_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            line_lower = line.lower()
            is_preferred_line = any(
                re.search(rf"\b{re.escape(marker)}\b", line_lower)
                for marker in preferred_line_markers
            )
            if is_preferred_line and "required" not in line_lower and "minimum" not in line_lower:
                continue

            for pat in patterns:
                for match in pat.finditer(line):
                    years_val = float(match.group(1))
                    if 0 < years_val <= 25:
                        strict_years.append(years_val)

        min_required_years: float | None = max(strict_years) if strict_years else None

        # Check Staff/Principal/Director/Fellow title when no explicit lower requirement was found
        title_lower = job_title.lower()
        is_staff_or_above = bool(
            re.search(
                r"\b(?:staff|principal|distinguished|fellow|vp|vice\s+president|director)\b",
                title_lower,
            )
            and "chief of staff" not in title_lower
            and "support staff" not in title_lower
        )
        if min_required_years is None and is_staff_or_above:
            min_required_years = STAFF_IMPLIED_MIN_YEARS

        if min_required_years is None:
            return (None, None)

        gap = min_required_years - self.estimated_years_experience
        if gap > tolerance_years:
            reason = (
                f"Experience mismatch blocker: Role requires {min_required_years:g}+ years "
                f"(or Staff/Principal seniority), while candidate profile estimates "
                f"{self.estimated_years_experience:g} years (tolerance: {tolerance_years:g}y)."
            )
            return (reason, min_required_years)

        return (None, min_required_years)

    def _detect_hard_blockers(
        self,
        job_info: dict[str, Any],
        tolerance_years: float,
    ) -> tuple[list[str], float | None]:
        """Run all deterministic hard-blocker checks against a normalized job payload."""
        jd_text = job_info["jd_text"]
        job_title = job_info["job_title"]
        location = job_info["location"]
        combined_text = f"{job_title}\n{location}\n{jd_text}".lower()

        blockers: list[str] = []

        sponsorship_blocker = self._check_sponsorship_blocker(combined_text)
        if sponsorship_blocker:
            blockers.append(sponsorship_blocker)

        citizenship_blocker = self._check_citizenship_blocker(combined_text)
        if citizenship_blocker:
            blockers.append(citizenship_blocker)

        clearance_blocker = self._check_clearance_blocker(combined_text)
        if clearance_blocker:
            blockers.append(clearance_blocker)

        country_blocker = self._check_country_auth_blocker(combined_text, location)
        if country_blocker:
            blockers.append(country_blocker)

        exp_blocker, min_req_years = self._check_experience_blocker(
            jd_text, job_title, tolerance_years
        )
        if exp_blocker:
            blockers.append(exp_blocker)

        return (blockers, min_req_years)

    def _candidate_has_skill(self, skill_name: str) -> bool:
        """Check whether candidate profile matches a requested skill or its aliases."""
        canonical = _canonicalize_skill_token(skill_name)
        keys_to_check = {skill_name.strip().lower(), canonical.lower()}

        for alias_canon, alias_tok, _ in _SKILL_PATTERNS:
            if alias_canon.lower() == canonical.lower():
                keys_to_check.add(alias_tok)

        if any(k in self.candidate_skill_keys for k in keys_to_check if k):
            return True

        # Fallback boundary search against candidate experience/project/story corpus
        corpus_lower = self.candidate_corpus.lower()
        for key in keys_to_check:
            if len(key) <= 2:
                continue
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])"
            if re.search(pattern, corpus_lower):
                return True

        return False

    def _extract_job_skills(self, job_info: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Extract prioritized (required_skills, preferred_skills) from job info."""
        explicit_req = [_canonicalize_skill_token(s) for s in job_info["required_skills"] if s.strip()]
        explicit_pref = [_canonicalize_skill_token(s) for s in job_info["preferred_skills"] if s.strip()]
        explicit_stack = [_canonicalize_skill_token(s) for s in job_info["tech_stack"] if s.strip()]

        jd_text = job_info["jd_text"]
        title = job_info["job_title"]
        text_skills = _extract_skills_from_text(f"{title}\n{jd_text}")

        # Split lines into preferred vs primary sections when parsing free-form JD
        preferred_from_text: set[str] = set()
        for line in jd_text.splitlines():
            lower_line = line.lower()
            if any(marker in lower_line for marker in ("preferred", "nice to have", "bonus", "plus")):
                for sk in _extract_skills_from_text(line):
                    preferred_from_text.add(sk)

        req_combined = _dedupe_preserve_order(
            explicit_req
            + explicit_stack
            + [sk for sk in text_skills if sk not in preferred_from_text]
        )
        req_lower = {s.lower() for s in req_combined}
        pref_combined = _dedupe_preserve_order(
            [s for s in explicit_pref + sorted(preferred_from_text) if s.lower() not in req_lower]
        )
        return (req_combined, pref_combined)

    def _evaluate_deterministic(
        self,
        job_info: dict[str, Any],
        tolerance_years: float,
    ) -> FitEvaluationResult:
        """Compute deterministic rule-based hard blockers and 1-5 fit score."""
        blockers, min_req_years = self._detect_hard_blockers(job_info, tolerance_years)
        hard_blocked = len(blockers) > 0

        req_skills, pref_skills = self._extract_job_skills(job_info)
        all_jd_skills = _dedupe_preserve_order(req_skills + pref_skills)

        matched_req = [s for s in req_skills if self._candidate_has_skill(s)]
        missing_req = [s for s in req_skills if not self._candidate_has_skill(s)]
        matched_pref = [s for s in pref_skills if self._candidate_has_skill(s)]
        missing_pref = [s for s in pref_skills if not self._candidate_has_skill(s)]

        matched_all = _dedupe_preserve_order(matched_req + matched_pref)
        missing_all = _dedupe_preserve_order(missing_req + missing_pref)

        # Weighted overlap ratio (required skills carry 2x weight over preferred skills)
        total_weight = (2.0 * len(req_skills)) + (1.0 * len(pref_skills))
        matched_weight = (2.0 * len(matched_req)) + (1.0 * len(matched_pref))

        title_lower = job_info["job_title"].lower()
        is_non_tech_title = any(kw in title_lower for kw in _NON_TECH_ROLE_KEYWORDS)
        is_tech_title = any(kw in title_lower for kw in _TECH_ROLE_KEYWORDS)

        if total_weight > 0:
            overlap_ratio = matched_weight / total_weight
            req_ratio = (len(matched_req) / len(req_skills)) if req_skills else overlap_ratio

            if overlap_ratio >= 0.80 and req_ratio >= 0.80 and len(missing_req) <= 1 and not is_non_tech_title:
                raw_score = 5
            elif overlap_ratio >= 0.55 and not is_non_tech_title:
                raw_score = 4
            elif overlap_ratio >= 0.35 and not is_non_tech_title:
                raw_score = 3
            elif overlap_ratio >= 0.15:
                raw_score = 2
            else:
                raw_score = 1
        else:
            # Generic JD with no explicit skill tokens detected
            if is_non_tech_title:
                raw_score = 1
            elif is_tech_title:
                raw_score = 3
            else:
                raw_score = 2

        if hard_blocked:
            # Cap blocked roles at 2 when technical overlap is otherwise strong, else 1
            fit_score = 2 if (raw_score >= 4 and len(blockers) == 1) else 1
            recommended_action: Literal["skip", "review", "priority_apply"] = "skip"
        else:
            fit_score = raw_score
            if fit_score == 5:
                recommended_action = "priority_apply"
            elif fit_score >= 3:
                recommended_action = "review"
            else:
                recommended_action = "skip"

        summary_parts: list[str] = []
        if hard_blocked:
            summary_parts.append(f"HARD BLOCKED ({len(blockers)}): {'; '.join(blockers)}")
        else:
            exp_note = (
                f"Meets experience requirement ({self.estimated_years_experience:g}y vs {min_req_years:g}+y required)."
                if min_req_years is not None
                else f"No blocking experience floor (candidate ~{self.estimated_years_experience:g}y)."
            )
            summary_parts.append(exp_note)

        if all_jd_skills:
            pct = int(round((len(matched_all) / len(all_jd_skills)) * 100))
            summary_parts.append(
                f"Skill overlap: {len(matched_all)}/{len(all_jd_skills)} ({pct}%). "
                f"Matched: {', '.join(matched_all[:8]) or 'none'}."
            )
            if missing_all:
                summary_parts.append(f"Gaps: {', '.join(missing_all[:6])}.")
        else:
            summary_parts.append("Few explicit technical skill tokens found in job description.")

        return FitEvaluationResult(
            fit_score=fit_score,
            hard_blocked=hard_blocked,
            blocker_reasons=blockers,
            matched_skills=matched_all,
            missing_skills=missing_all,
            evaluation_summary=" ".join(summary_parts),
            recommended_action=recommended_action,
        )

    def _evaluate_with_gemini(
        self,
        job_info: dict[str, Any],
        client: GeminiClient,
        baseline: FitEvaluationResult,
    ) -> FitEvaluationResult:
        """Enhance fit evaluation via Gemini structured output while preserving hard blockers."""
        system_instruction = (
            "You are a strict technical recruiting evaluator. Score candidate-to-job fit from 1 to 5:\n"
            "- 5: Direct technical & architectural match, zero hard blockers, very high skill overlap.\n"
            "- 3-4: Strong skills overlap with minor gaps, zero hard blockers.\n"
            "- 1-2: Significant stack mismatch OR any hard blocker triggered (sponsorship, citizenship, "
            "clearance, or strict minimum years mismatch).\n"
            "If any hard blocker applies, set hard_blocked=true, fit_score<=2, and recommended_action='skip'.\n"
            "Never invent skills not present in the candidate profile."
        )

        prompt = (
            f"CANDIDATE PROFILE SUMMARY:\n{self.candidate_summary}\n"
            f"Estimated Experience: {self.estimated_years_experience:g} years\n"
            f"Skills: {', '.join(self.candidate_skills)}\n"
            f"Work Authorization: sponsorship_required={self.work_authorization['sponsorship_required']}, "
            f"future_sponsorship_required={self.work_authorization['future_sponsorship_required']}, "
            f"countries_authorized={sorted(self.work_authorization['countries_authorized'])}, "
            f"security_clearance={self.work_authorization['security_clearance']}, "
            f"is_us_citizen={self.work_authorization['is_us_citizen']}\n\n"
            f"TARGET JOB:\n"
            f"Title: {job_info['job_title']}\n"
            f"Company: {job_info['company']}\n"
            f"Location: {job_info['location']}\n"
            f"Description:\n{job_info['jd_text'][:6000]}\n\n"
            f"DETERMINISTIC HARD BLOCKERS DETECTED: {baseline.blocker_reasons}\n"
        )

        llm_result = client.generate_structured(
            prompt=prompt,
            response_schema=FitEvaluationResult,
            system_instruction=system_instruction,
            temperature=0.1,
        )

        merged_blockers = _dedupe_preserve_order(baseline.blocker_reasons + llm_result.blocker_reasons)
        is_blocked = baseline.hard_blocked or llm_result.hard_blocked or bool(merged_blockers)
        score = min(max(1, llm_result.fit_score), 2) if is_blocked else llm_result.fit_score
        action: Literal["skip", "review", "priority_apply"] = (
            "skip" if (is_blocked or score <= 2) else ("priority_apply" if score == 5 else llm_result.recommended_action)
        )

        return FitEvaluationResult(
            fit_score=score,
            hard_blocked=is_blocked,
            blocker_reasons=merged_blockers,
            matched_skills=llm_result.matched_skills or baseline.matched_skills,
            missing_skills=llm_result.missing_skills or baseline.missing_skills,
            evaluation_summary=llm_result.evaluation_summary or baseline.evaluation_summary,
            recommended_action=action,
        )

    def evaluate_fit(
        self,
        job: Any,
        *,
        job_title: str = "",
        company: str = "",
        location: str = "",
        required_skills: list[str] | None = None,
        preferred_skills: list[str] | None = None,
        gemini_client: GeminiClient | None = None,
        use_gemini: bool = True,
        experience_tolerance_years: float | None = None,
    ) -> FitEvaluationResult:
        """Evaluate fit score (1-5) and hard blockers for a job posting.

        Args:
            job: Target job as ``JobProfileSchema``, queue dict, or raw JD text.
            job_title: Optional job title override.
            company: Optional company name override.
            location: Optional job location override.
            required_skills: Optional explicit required skills list.
            preferred_skills: Optional explicit preferred skills list.
            gemini_client: Optional ``GeminiClient`` override for LLM evaluation.
            use_gemini: Set False to force deterministic offline scoring.
            experience_tolerance_years: Optional override for experience gap tolerance.

        Returns:
            Validated ``FitEvaluationResult`` instance.
        """
        tolerance = (
            self.experience_tolerance_years
            if experience_tolerance_years is None
            else max(0.0, float(experience_tolerance_years))
        )
        job_info = self._normalize_job_input(
            job,
            job_title=job_title,
            company=company,
            location=location,
            required_skills=required_skills,
            preferred_skills=preferred_skills,
        )

        baseline = self._evaluate_deterministic(job_info, tolerance)
        active_client = gemini_client if gemini_client is not None else self.gemini_client

        if use_gemini and active_client is not None:
            try:
                return self._evaluate_with_gemini(job_info, active_client, baseline)
            except Exception as exc:
                logger.warning("Gemini fit evaluation failed (%s); using deterministic fallback.", exc)

        return baseline

    def score_intake_queue(
        self,
        queue_path: Path | str | None = None,
        min_score_to_keep: int = 1,
        *,
        rescore_all: bool = False,
        gemini_client: GeminiClient | None = None,
        use_gemini: bool = True,
        prune_below_min: bool = False,
    ) -> dict[str, Any]:
        """Score unscored entries in intake_queue.json and write updates atomically.

        Args:
            queue_path: Path to intake queue JSON. Defaults to
                ``Path(os.getenv("HISTORY_PATH", "history")) / "intake_queue.json"``.
            min_score_to_keep: Minimum fit score (1-5) considered actionable.
            rescore_all: If True, re-evaluate already-scored jobs as well.
            gemini_client: Optional ``GeminiClient`` override.
            use_gemini: Set False to force deterministic offline scoring.
            prune_below_min: If True, drop blocked or below-threshold jobs from the saved list.

        Returns:
            Summary dictionary with counts, ``kept_jobs``, and updated ``jobs`` list.
        """
        resolved_path = Path(queue_path) if queue_path is not None else resolve_default_queue_path()

        if not resolved_path.is_file():
            logger.warning("Intake queue file not found at '%s'.", resolved_path)
            return {
                "queue_path": str(resolved_path),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "total": 0,
                "newly_scored": 0,
                "scored_count": 0,
                "blocked_count": 0,
                "skipped_count": 0,
                "kept_count": 0,
                "kept_jobs": [],
                "jobs": [],
            }

        raw_text = resolved_path.read_text(encoding="utf-8").strip()
        parsed_root: Any = json.loads(raw_text) if raw_text else {"jobs": []}

        root_is_list = isinstance(parsed_root, list)
        if root_is_list:
            jobs_raw = parsed_root
            envelope: dict[str, Any] = {}
        elif isinstance(parsed_root, dict):
            envelope = dict(parsed_root)
            jobs_raw = parsed_root.get("jobs", [])
            if not isinstance(jobs_raw, list):
                jobs_raw = []
        else:
            raise ValueError(f"Unsupported intake queue JSON structure in '{resolved_path}'.")

        newly_scored = 0
        updated_jobs: list[dict[str, Any]] = []

        for entry in jobs_raw:
            if not isinstance(entry, dict):
                continue
            job_item = dict(entry)
            current_status = str(job_item.get("status", "")).strip().lower()
            already_scored = (
                job_item.get("fit_score") is not None
                and current_status not in ("", "discovered", "pending", "unscored")
            )

            if rescore_all or not already_scored:
                result = self.evaluate_fit(
                    job_item,
                    gemini_client=gemini_client,
                    use_gemini=use_gemini,
                )
                job_item["fit_score"] = result.fit_score
                job_item["hard_blocked"] = result.hard_blocked
                job_item["blocker_reasons"] = result.blocker_reasons
                job_item["matched_skills"] = result.matched_skills
                job_item["missing_skills"] = result.missing_skills
                job_item["evaluation_summary"] = result.evaluation_summary
                job_item["recommended_action"] = result.recommended_action

                job_item["status"] = "blocked" if result.hard_blocked else "scored"
                newly_scored += 1

            updated_jobs.append(job_item)

        kept_jobs = [
            j for j in updated_jobs
            if not j.get("hard_blocked", False)
            and int(j.get("fit_score") or 0) >= min_score_to_keep
        ]
        persisted_jobs = kept_jobs if prune_below_min else updated_jobs

        now_iso = datetime.now(timezone.utc).isoformat()
        payload_to_write: Any
        if root_is_list:
            payload_to_write = persisted_jobs
        else:
            envelope["updated_at"] = now_iso
            envelope["jobs"] = persisted_jobs
            payload_to_write = envelope

        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload_to_write, indent=2, ensure_ascii=False)
        tmp_path = resolved_path.with_name(f".{resolved_path.name}.{secrets.token_hex(4)}.tmp")
        try:
            tmp_path.write_text(serialized, encoding="utf-8")
            tmp_path.replace(resolved_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        blocked_count = sum(1 for j in persisted_jobs if j.get("status") == "blocked")
        skipped_count = sum(1 for j in persisted_jobs if j.get("status") == "skipped")
        scored_count = sum(1 for j in persisted_jobs if j.get("status") == "scored")

        return {
            "queue_path": str(resolved_path),
            "updated_at": now_iso,
            "total": len(persisted_jobs),
            "newly_scored": newly_scored,
            "scored_count": scored_count,
            "blocked_count": blocked_count,
            "skipped_count": skipped_count,
            "kept_count": len(kept_jobs),
            "kept_jobs": kept_jobs,
            "jobs": persisted_jobs,
        }


def score_intake_queue(
    queue_path: Path | str | None = None,
    min_score_to_keep: int = 1,
    *,
    profile_path: Path | str | None = None,
    profile_data: dict[str, Any] | None = None,
    gemini_client: GeminiClient | None = None,
    use_gemini: bool = True,
    rescore_all: bool = False,
    prune_below_min: bool = False,
    experience_tolerance_years: float = DEFAULT_EXPERIENCE_TOLERANCE_YEARS,
) -> dict[str, Any]:
    """Convenience wrapper instantiating ``JobFitScorer`` to score ``intake_queue.json``."""
    scorer = JobFitScorer(
        profile_path=profile_path,
        profile_data=profile_data,
        gemini_client=gemini_client,
        experience_tolerance_years=experience_tolerance_years,
    )
    return scorer.score_intake_queue(
        queue_path=queue_path,
        min_score_to_keep=min_score_to_keep,
        rescore_all=rescore_all,
        gemini_client=gemini_client,
        use_gemini=use_gemini,
        prune_below_min=prune_below_min,
    )


__all__ = [
    "CLEARANCE_LEVELS",
    "DEFAULT_EXPERIENCE_TOLERANCE_YEARS",
    "FitEvaluationResult",
    "JobFitScorer",
    "resolve_default_profile_path",
    "resolve_default_queue_path",
    "score_intake_queue",
]
