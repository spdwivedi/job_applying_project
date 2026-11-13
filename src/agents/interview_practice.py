"""Interactive Interview Practice Mode agent for JobApplier-Core.

Loads company application dossiers and candidate ground-truth profile stories,
runs simulated behavioral and technical gap interviews via API or CLI, evaluates
candidate responses across the STAR dimensions (Situation, Task, Action, Result),
and recommends concrete supporting evidence from the candidate's real profile.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from src.engine.llm import GeminiClient

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_DIR_NAME: Final[str] = "history"
DEFAULT_VAULT_DIR_NAME: Final[str] = "vault"
SUPPORTED_ROLE_CATEGORIES: Final[tuple[str, str]] = ("full_time", "internships")
SUPPORTED_MODES: Final[tuple[str, ...]] = ("mixed", "behavioral", "technical")

INTERVIEW_PREP_DIR: Final[str] = "interview_prep"
BEHAVIORAL_QA_FILENAME: Final[str] = "targeted_behavioral_qa.md"
TECHNICAL_GAP_FILENAME: Final[str] = "technical_skills_gap.md"
COMPANY_BRIEFING_FILENAME: Final[str] = "company_briefing.md"
RAW_JD_FILENAME: Final[str] = "raw_job_description.md"
META_FILENAME: Final[str] = "application_meta.json"
MASTER_PROFILE_FILENAME: Final[str] = "master_profile.yaml"

_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "about",
        "tell",
        "describe",
        "time",
        "work",
        "worked",
        "using",
        "used",
    }
)

_COMMON_TECH_TERMS: Final[tuple[str, ...]] = (
    "python",
    "typescript",
    "javascript",
    "go",
    "rust",
    "java",
    "c++",
    "sql",
    "fastapi",
    "django",
    "flask",
    "react",
    "next.js",
    "node.js",
    "graphql",
    "grpc",
    "rest",
    "postgresql",
    "postgres",
    "mysql",
    "redis",
    "redis streams",
    "mongodb",
    "elasticsearch",
    "dynamodb",
    "kafka",
    "rabbitmq",
    "docker",
    "kubernetes",
    "terraform",
    "aws",
    "gcp",
    "azure",
    "github actions",
    "ci/cd",
    "playwright",
    "pytest",
    "pytorch",
    "tensorflow",
    "async",
    "asyncio",
    "microservices",
    "distributed systems",
    "caching",
    "idempotency",
    "backpressure",
    "rate limiting",
    "connection pooling",
    "indexing",
    "canary",
    "rollback",
    "encryption",
    "aes-256-gcm",
    "envelope encryption",
    "telemetry",
    "observability",
)


class InterviewQuestion(BaseModel):
    """Structured representation of a single interview practice question."""

    id: str = Field(default="", description="Unique identifier for the question in a session.")
    question: str = Field(..., description="The exact interview prompt posed to the candidate.")
    category: Literal["behavioral", "technical"] = Field(
        default="behavioral",
        description="Question classification: 'behavioral' or 'technical'.",
    )
    topic: str = Field(
        default="",
        description="Competency, skill gap, or technical domain targeted by the question.",
    )
    source_doc: str = Field(
        default="",
        description="Prep document filename from which this question was extracted.",
    )
    focus_points: list[str] = Field(
        default_factory=list,
        description="Expected engineering concepts, competencies, or rubric cues.",
    )
    mapped_story_id: str = Field(
        default="",
        description="Optional ID or title of a pre-mapped story from prep docs.",
    )

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, value: Any) -> str:
        if not isinstance(value, str):
            return "behavioral"
        lowered = value.strip().lower()
        if any(tok in lowered for tok in ("tech", "system", "arch", "gap", "coding", "skill")):
            return "technical"
        return "behavioral"


class STAREvaluation(BaseModel):
    """Structured STAR rubric evaluation and profile-grounded coaching feedback."""

    question_id: str = Field(default="", description="Identifier of the evaluated question.")
    question_text: str = Field(default="", description="Prompt text that was answered.")
    answer_text: str = Field(default="", description="Candidate answer submitted for evaluation.")
    situation_score: int = Field(
        ...,
        ge=1,
        le=5,
        description="Score (1-5) for Situation context clarity and stakes.",
    )
    task_score: int = Field(
        ...,
        ge=1,
        le=5,
        description="Score (1-5) for Task/objective definition and personal ownership.",
    )
    action_score: int = Field(
        ...,
        ge=1,
        le=5,
        description="Score (1-5) for specific individual Actions and technical depth.",
    )
    result_score: int = Field(
        ...,
        ge=1,
        le=5,
        description="Score (1-5) for measurable Results, metrics, and business impact.",
    )
    overall_score: float = Field(
        ...,
        ge=1.0,
        le=5.0,
        description="Overall STAR score on a 1.0 to 5.0 scale.",
    )
    strengths: list[str] = Field(
        default_factory=list,
        description="Concrete strengths observed in the candidate's answer.",
    )
    gaps: list[str] = Field(
        default_factory=list,
        description="Missing STAR dimensions, vague claims, or technical gaps.",
    )
    coaching_feedback: str = Field(
        default="",
        description="Actionable coaching guidance to strengthen the response.",
    )
    suggested_evidence: list[str] = Field(
        default_factory=list,
        description="Real stories, experiences, projects, and metrics from the candidate profile.",
    )
    evaluation_mode: Literal["gemini", "offline"] = Field(
        default="offline",
        description="Evaluation backend used ('gemini' or 'offline').",
    )


class PracticeTurn(BaseModel):
    """Completed Q&A evaluation turn recorded inside a practice session."""

    question: InterviewQuestion
    answer: str
    evaluation: STAREvaluation
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class PracticeSessionState(BaseModel):
    """State container tracking an active or completed interview practice session."""

    session_id: str = Field(default_factory=lambda: secrets.token_hex(6))
    company: str = Field(default="Target Company")
    role: str = Field(default="Target Role")
    role_category: str = Field(default="full_time")
    dossier_dir: str = Field(default="")
    mode: Literal["mixed", "behavioral", "technical"] = Field(default="mixed")
    max_questions: int = Field(default=5, ge=1)
    questions: list[InterviewQuestion] = Field(default_factory=list)
    current_index: int = Field(default=0, ge=0)
    current_question: InterviewQuestion | None = Field(default=None)
    turns: list[PracticeTurn] = Field(default_factory=list)
    evaluations: list[STAREvaluation] = Field(default_factory=list)
    completed: bool = Field(default=False)
    job_description_excerpt: str = Field(default="")
    skills_gap_summary: str = Field(default="")
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class _ExtractedQuestionsBatch(BaseModel):
    """Internal Gemini structured schema for extracting interview questions."""

    questions: list[InterviewQuestion] = Field(default_factory=list)


def _tokenize(text: str) -> set[str]:
    """Tokenize text into normalized meaningful lowercase keywords."""
    if not text:
        return set()
    expanded = text.replace("_", " ").replace("-", " ")
    tokens = re.findall(r"[a-z0-9][a-z0-9.+/#]*", expanded.lower())
    return {tok for tok in tokens if len(tok) > 1 and tok not in _STOPWORDS}


def _strip_markdown_wrapping(text: str) -> str:
    """Strip markdown bold, italic, and outer quotation wrapping."""
    cleaned = text.strip()
    # Remove outer bold/italic wrappers repeatedly
    for _ in range(3):
        cleaned = cleaned.strip()
        if (cleaned.startswith("**") and cleaned.endswith("**")) or (
            cleaned.startswith("__") and cleaned.endswith("__")
        ):
            cleaned = cleaned[2:-2].strip()
        elif (cleaned.startswith("*") and cleaned.endswith("*")) or (
            cleaned.startswith("_") and cleaned.endswith("_")
        ):
            cleaned = cleaned[1:-1].strip()
        elif (cleaned.startswith('"') and cleaned.endswith('"')) or (
            cleaned.startswith("'") and cleaned.endswith("'")
        ):
            cleaned = cleaned[1:-1].strip()
        else:
            break
    return cleaned.strip()


def _looks_like_question_or_prompt(text: str) -> bool:
    """Check whether text reads like an interview prompt or question."""
    cleaned = _strip_markdown_wrapping(text)
    if len(cleaned) < 15:
        return False
    if cleaned.endswith("?"):
        return True
    lowered = cleaned.lower()
    starters = (
        "tell me",
        "describe",
        "walk me through",
        "explain",
        "how would",
        "how did",
        "how do",
        "why ",
        "what ",
        "when ",
        "give me an example",
        "share a time",
    )
    return lowered.startswith(starters)


def _extract_metrics_snippets(text: str) -> list[str]:
    """Extract quantified metric phrases from text."""
    patterns = (
        r"\d+(?:\.\d+)?\s*%",
        r"\d+(?:\.\d+)?\s*[xX]\b",
        r"\$\s*\d+(?:,\d{3})*(?:\.\d+)?\s*(?:[kmb]|million|billion|thousand)?\b",
        r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|s|sec|seconds?|minutes?|hours?|days?|weeks?)\b",
        r"\b\d+(?:,\d{3})*(?:\.\d+)?\s*(?:k|m|b)?\+?\s+(?:daily|weekly|monthly|active|events|users|requests|repositories|teams|findings)\b",
        r"\bzero\s+(?:downtime|findings|regressions|errors)\b",
    )
    found: list[str] = []
    for pat in patterns:
        for match in re.finditer(pat, text, re.IGNORECASE):
            val = match.group(0).strip()
            if val not in found:
                found.append(val)
    return found


def _infer_meta_from_markdown(md_text: str) -> tuple[str, str]:
    """Extract company and role strings from markdown header lines if available."""
    company = ""
    role = ""
    if not md_text:
        return company, role

    comp_match = re.search(
        r"\*\*(?:Hiring Company|Target Company|Company):\*\*[ \t]*(.+)",
        md_text,
        re.IGNORECASE,
    )
    if comp_match:
        company = comp_match.group(1).strip()

    role_match = re.search(
        r"\*\*(?:Target Role|Role|Job Title):\*\*[ \t]*(.+)",
        md_text,
        re.IGNORECASE,
    )
    if role_match:
        role = role_match.group(1).strip()

    if not company or not role:
        title_match = re.search(r"^#\s+(.+?)\s+at\s+(.+)$", md_text, re.MULTILINE)
        if title_match:
            role = role or title_match.group(1).strip()
            company = company or title_match.group(2).strip()

    return company, role


class InterviewPracticeAgent:
    """Interactive Interview Practice simulator and STAR coaching agent."""

    def __init__(
        self,
        history_root: Path | str | None = None,
        master_profile_path: Path | str | None = None,
        profile_data: dict[str, Any] | None = None,
        gemini_client: GeminiClient | None = None,
    ) -> None:
        """Initialize the InterviewPracticeAgent.

        Args:
            history_root: Root directory containing archived applications under
                full_time and internships. Defaults to HISTORY_PATH env var or 'history'.
            master_profile_path: Explicit path to candidate master_profile.yaml.
                Defaults to VAULT_PATH env var / 'master_profile.yaml' (with .example fallback).
            profile_data: Optional pre-loaded profile dictionary for offline or test usage.
            gemini_client: Optional GeminiClient instance for LLM question extraction & STAR scoring.
        """
        if history_root is None:
            self.history_root = Path(os.getenv("HISTORY_PATH", DEFAULT_HISTORY_DIR_NAME))
        else:
            self.history_root = Path(history_root)

        if master_profile_path is None:
            vault_dir = Path(os.getenv("VAULT_PATH", DEFAULT_VAULT_DIR_NAME))
            self.master_profile_path = vault_dir / MASTER_PROFILE_FILENAME
        else:
            self.master_profile_path = Path(master_profile_path)

        self.gemini_client = gemini_client

        self._profile_data: dict[str, Any] = {}
        self.cv: dict[str, Any] = {}
        self.story_bank: list[dict[str, Any]] = []
        self.experience: list[dict[str, Any]] = []
        self.projects: list[dict[str, Any]] = []
        self.skills: list[str] = []
        self.summary: list[str] = []

        if profile_data is not None:
            self.load_profile_data(profile_data)
        else:
            self.load_profile()

    def load_profile(self) -> None:
        """Load candidate profile from master_profile.yaml (falling back to .example if needed)."""
        target_path = self.master_profile_path
        if not target_path.is_file():
            example_candidate = target_path.with_name(target_path.name + ".example")
            if example_candidate.is_file():
                target_path = example_candidate

        if target_path.is_file():
            try:
                raw_yaml = target_path.read_text(encoding="utf-8")
                parsed = yaml.safe_load(raw_yaml) or {}
                if not isinstance(parsed, dict):
                    parsed = {}
                self.load_profile_data(parsed)
                return
            except Exception:
                logger.exception("Failed to parse master profile YAML from '%s'.", target_path)
        else:
            logger.warning("Master profile YAML not found at '%s'.", target_path)

        self.load_profile_data({})

    def load_profile_data(self, data: dict[str, Any]) -> None:
        """Populate story_bank, experience, projects, and skills from a profile dictionary."""
        self._profile_data = data if isinstance(data, dict) else {}
        self.cv = self._profile_data.get("cv", {}) if isinstance(self._profile_data.get("cv"), dict) else {}
        sections = self.cv.get("sections", {}) if isinstance(self.cv.get("sections"), dict) else {}

        raw_stories = self._profile_data.get("story_bank", [])
        self.story_bank = [s for s in raw_stories if isinstance(s, dict)] if isinstance(raw_stories, list) else []

        raw_exp = sections.get("experience", self._profile_data.get("experience", []))
        self.experience = [e for e in raw_exp if isinstance(e, dict)] if isinstance(raw_exp, list) else []

        raw_proj = sections.get("projects", self._profile_data.get("projects", []))
        self.projects = [p for p in raw_proj if isinstance(p, dict)] if isinstance(raw_proj, list) else []

        raw_summary = sections.get("summary", [])
        self.summary = [str(item).strip() for item in raw_summary if str(item).strip()] if isinstance(raw_summary, list) else []

        extracted_skills: list[str] = []
        raw_skills = sections.get("skills", self._profile_data.get("skills", []))
        if isinstance(raw_skills, list):
            for entry in raw_skills:
                if isinstance(entry, dict):
                    details = str(entry.get("details", ""))
                    for tok in re.split(r"[,;/|]", details):
                        cleaned = tok.strip()
                        if cleaned:
                            extracted_skills.append(cleaned)
                elif isinstance(entry, str) and entry.strip():
                    extracted_skills.append(entry.strip())
        self.skills = extracted_skills

    def resolve_dossier_dir(self, dossier_dir_or_slug: Path | str) -> Path:
        """Resolve a direct dossier directory path or search by company slug across categories.

        Searches under `history_root` inside `full_time` and `internships` (and directly under
        `history_root` as a fallback).

        Args:
            dossier_dir_or_slug: Direct Path to a dossier folder, or company name / folder slug.

        Returns:
            Resolved Path to the target dossier directory.

        Raises:
            ValueError: If input is empty.
            FileNotFoundError: If no matching dossier directory can be located.
        """
        if dossier_dir_or_slug is None:
            raise ValueError("dossier_dir_or_slug must not be None.")

        raw_str = str(dossier_dir_or_slug).strip()
        if not raw_str:
            raise ValueError("dossier_dir_or_slug must be a non-empty path or company slug.")

        direct_candidate = Path(dossier_dir_or_slug)
        if direct_candidate.is_dir():
            has_dossier_markers = (
                (direct_candidate / INTERVIEW_PREP_DIR).is_dir()
                or (direct_candidate / META_FILENAME).is_file()
                or (direct_candidate / RAW_JD_FILENAME).is_file()
            )
            if isinstance(dossier_dir_or_slug, Path) or has_dossier_markers or direct_candidate.is_absolute():
                return direct_candidate

        query_lower = raw_str.lower()
        query_slug = re.sub(r"[^a-z0-9]+", "_", query_lower).strip("_")

        search_roots: list[Path] = [
            self.history_root / category for category in SUPPORTED_ROLE_CATEGORIES
        ]
        search_roots.append(self.history_root)

        scored_candidates: list[tuple[int, str, Path]] = []
        seen_paths: set[Path] = set()

        for root in search_roots:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if not entry.is_dir() or entry in seen_paths:
                    continue
                if entry.name in SUPPORTED_ROLE_CATEGORIES and root == self.history_root:
                    continue
                seen_paths.add(entry)

                folder_lower = entry.name.lower()
                folder_slug = re.sub(r"[^a-z0-9]+", "_", folder_lower).strip("_")

                meta_company = ""
                meta_slug = ""
                meta_file = entry / META_FILENAME
                if meta_file.is_file():
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        if isinstance(meta, dict):
                            meta_company = str(meta.get("company", "")).strip()
                            meta_slug = str(meta.get("slug", "")).strip()
                    except Exception:
                        pass

                meta_company_slug = re.sub(r"[^a-z0-9]+", "_", meta_company.lower()).strip("_")

                score = 0
                if folder_lower == query_lower or folder_slug == query_slug or meta_slug.lower() == query_lower:
                    score = 100
                elif meta_company and (
                    meta_company.lower() == query_lower or meta_company_slug == query_slug
                ):
                    score = 90
                elif query_slug and (
                    f"_{query_slug}_" in f"_{folder_slug}_" or folder_slug.endswith(f"_{query_slug}")
                ):
                    score = 75
                elif query_slug and query_slug in folder_slug:
                    score = 60
                elif meta_company and query_lower in meta_company.lower():
                    score = 55

                if score > 0:
                    scored_candidates.append((score, entry.name, entry))

        if scored_candidates:
            scored_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return scored_candidates[0][2]

        if direct_candidate.is_dir():
            return direct_candidate

        raise FileNotFoundError(
            f"Could not resolve application dossier for '{raw_str}' under '{self.history_root}'."
        )

    def load_context(self, dossier_dir_or_slug: Path | str) -> dict[str, Any]:
        """Load interview prep markdown docs, job description, and metadata for a dossier."""
        dossier_dir = self.resolve_dossier_dir(dossier_dir_or_slug)
        prep_dir = dossier_dir / INTERVIEW_PREP_DIR

        behavioral_path = prep_dir / BEHAVIORAL_QA_FILENAME
        skills_gap_path = prep_dir / TECHNICAL_GAP_FILENAME
        briefing_path = prep_dir / COMPANY_BRIEFING_FILENAME
        jd_path = dossier_dir / RAW_JD_FILENAME
        meta_path = dossier_dir / META_FILENAME

        behavioral_md = behavioral_path.read_text(encoding="utf-8") if behavioral_path.is_file() else ""
        skills_gap_md = skills_gap_path.read_text(encoding="utf-8") if skills_gap_path.is_file() else ""
        briefing_md = briefing_path.read_text(encoding="utf-8") if briefing_path.is_file() else ""
        raw_jd_md = jd_path.read_text(encoding="utf-8") if jd_path.is_file() else ""

        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                loaded_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(loaded_meta, dict):
                    meta = loaded_meta
            except Exception as exc:
                logger.warning("Failed to parse %s: %s", meta_path, exc)

        inferred_comp, inferred_role = _infer_meta_from_markdown(behavioral_md or skills_gap_md or raw_jd_md)

        company = str(meta.get("company", "")).strip() or inferred_comp
        role = str(meta.get("role", "")).strip() or inferred_role

        if not company:
            # Fallback: extract token after YYYY-MM-DD_ prefix in folder name
            parts = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", dossier_dir.name).split("_")
            company = parts[0].title() if parts and parts[0] else "Target Company"
        if not role:
            role = "Target Role"

        role_category = str(meta.get("role_category", "")).strip()
        if not role_category:
            parent_name = dossier_dir.parent.name.lower()
            role_category = parent_name if parent_name in SUPPORTED_ROLE_CATEGORIES else "full_time"

        return {
            "dossier_dir": dossier_dir,
            "company": company,
            "role": role,
            "role_category": role_category,
            "application_meta": meta,
            "targeted_behavioral_qa": behavioral_md,
            "technical_skills_gap": skills_gap_md,
            "company_briefing": briefing_md,
            "raw_job_description": raw_jd_md,
        }

    # ------------------------------------------------------------------
    # Question Extraction (Offline Markdown + Gemini)
    # ------------------------------------------------------------------
    def _parse_behavioral_questions(self, markdown: str) -> list[InterviewQuestion]:
        """Parse targeted behavioral interview questions from targeted_behavioral_qa.md."""
        if not markdown.strip():
            return []

        questions: list[InterviewQuestion] = []
        seen_prompts: dict[str, int] = {}

        # Split by level-3 question headers (e.g., ### Question 1: ... or ### Q1: ...)
        header_pattern = re.compile(
            r"^#{2,4}[ \t]+(?:Question|Q)[ \t]*\d*[ \t]*[:\-\.]?[ \t]*(.+)$",
            re.IGNORECASE | re.MULTILINE,
        )
        matches = list(header_pattern.finditer(markdown))

        for idx, match in enumerate(matches):
            topic = _strip_markdown_wrapping(match.group(1))
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
            block = markdown[start:end]

            # Stop before a top-level section boundary like `## 3.` or `---`
            boundary = re.search(r"\n(?:---|\#{1,2}\s+\d+\.)", block)
            if boundary:
                block = block[: boundary.start()]

            prompt_text = ""
            prompt_match = re.search(
                r"(?:Anticipated Screening Prompt|Screening Prompt|Prompt|Question)[ \t]*:[ \t]*\**[ \t]*(.+)",
                block,
                re.IGNORECASE,
            )
            if prompt_match:
                prompt_text = _strip_markdown_wrapping(prompt_match.group(1))
            else:
                # Look for quoted text or explicit question sentence in the block
                quote_match = re.search(r'[*_"]*"([^"\n]{15,})"[*_"]*', block)
                if quote_match and _looks_like_question_or_prompt(quote_match.group(1)):
                    prompt_text = _strip_markdown_wrapping(quote_match.group(1))
                else:
                    for line in block.splitlines():
                        candidate_line = re.sub(r"^\s*[-*]\s*", "", line).strip()
                        if _looks_like_question_or_prompt(candidate_line):
                            prompt_text = _strip_markdown_wrapping(candidate_line)
                            break

            if not prompt_text:
                continue

            # Extract mapped story ID or title if present
            mapped_story_id = ""
            story_match = re.search(
                r"Mapped STAR Story[ \t]*:[ \t]*\**[ \t]*([^`\n]+?)(?:[ \t]*\(`([^`]+)`\))?[ \t]*$",
                block,
                re.IGNORECASE | re.MULTILINE,
            )
            if story_match:
                mapped_story_id = (story_match.group(2) or story_match.group(1) or "").strip().strip("*")

            # Extract focus points from competencies inside topic + delivery strategy
            focus_points: list[str] = []
            comp_in_parens = re.search(r"\(([^)]+)\)", topic)
            if comp_in_parens:
                for comp in comp_in_parens.group(1).split(","):
                    cleaned_c = comp.strip().replace("_", " ")
                    if cleaned_c:
                        focus_points.append(cleaned_c)
            elif topic:
                focus_points.append(topic)

            strat_match = re.search(
                r"Delivery Strategy[ \t]*:[ \t]*\**[ \t]*(.+)",
                block,
                re.IGNORECASE,
            )
            if strat_match:
                focus_points.append(_strip_markdown_wrapping(strat_match.group(1)))

            # If template reused identical prompt across distinct competency sections, specialize it
            norm_key = prompt_text.lower()
            occurrence = seen_prompts.get(norm_key, 0)
            seen_prompts[norm_key] = occurrence + 1
            if occurrence > 0:
                comp_label = (
                    comp_in_parens.group(1).replace("_", " ").strip()
                    if comp_in_parens
                    else topic
                )
                if comp_label:
                    prompt_text = (
                        f"Tell me about a time you demonstrated {comp_label} "
                        "in an engineering project or high-stakes technical situation."
                    )

            questions.append(
                InterviewQuestion(
                    question=prompt_text,
                    category="behavioral",
                    topic=topic or "Behavioral Competency",
                    source_doc=BEHAVIORAL_QA_FILENAME,
                    focus_points=focus_points,
                    mapped_story_id=mapped_story_id,
                )
            )

        # Fallback: parse numbered or bulleted prompts inside question sections if no ### Question headers
        if not questions:
            in_question_section = True
            for line in markdown.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    heading_lower = stripped.lower()
                    in_question_section = any(
                        kw in heading_lower
                        for kw in ("question", "prompt", "behavioral", "predicted", "scenario")
                    ) and "guideline" not in heading_lower
                    continue
                if not in_question_section:
                    continue
                cleaned_line = re.sub(r"^(?:\d+[.)]|[-*])\s*", "", stripped).strip()
                cleaned_line = re.sub(r"^\*\*[^*]+\*\*\s*:?\s*", "", cleaned_line).strip()
                if _looks_like_question_or_prompt(cleaned_line):
                    questions.append(
                        InterviewQuestion(
                            question=_strip_markdown_wrapping(cleaned_line),
                            category="behavioral",
                            topic="Behavioral Screening",
                            source_doc=BEHAVIORAL_QA_FILENAME,
                        )
                    )

        return questions

    def _parse_technical_questions(
        self,
        markdown: str,
        *,
        company: str,
        role: str,
    ) -> list[InterviewQuestion]:
        """Parse technical screening questions and gap focus areas from technical_skills_gap.md."""
        if not markdown.strip():
            return []

        screening_qs: list[InterviewQuestion] = []
        gap_qs: list[InterviewQuestion] = []
        study_qs: list[InterviewQuestion] = []
        seen_questions: set[str] = set()

        def _make_tech_question(
            target_list: list[InterviewQuestion],
            q_text: str,
            topic: str,
            focus_points: list[str] | None = None,
        ) -> None:
            cleaned = _strip_markdown_wrapping(q_text)
            key = cleaned.lower()
            if cleaned and key not in seen_questions:
                seen_questions.add(key)
                target_list.append(
                    InterviewQuestion(
                        question=cleaned,
                        category="technical",
                        topic=topic or "Technical Architecture",
                        source_doc=TECHNICAL_GAP_FILENAME,
                        focus_points=focus_points or [],
                    )
                )

        # 1. Extract explicit Anticipated Technical Screening Questions section
        tech_sec_match = re.search(
            r"^#{1,3}\s+.*?(?:Anticipated\s+Technical|Technical\s+(?:Screening\s+)?Questions|Practice\s+Questions).*?$",
            markdown,
            re.IGNORECASE | re.MULTILINE,
        )
        if tech_sec_match:
            sec_start = tech_sec_match.end()
            next_h2 = re.search(r"\n#{1,2}\s+", markdown[sec_start:])
            sec_body = markdown[sec_start : sec_start + next_h2.start()] if next_h2 else markdown[sec_start:]

            # Match numbered blocks: `1. **Topic:** ...`
            num_blocks = list(re.finditer(r"(?m)^[ \t]*\d+\.[ \t]+\*\*([^*]+?)\*\*:?[ \t]*(.*)$", sec_body))
            if num_blocks:
                for idx, nb in enumerate(num_blocks):
                    topic = _strip_markdown_wrapping(nb.group(1)).rstrip(":")
                    inline_rest = _strip_markdown_wrapping(nb.group(2))
                    b_start = nb.end()
                    b_end = num_blocks[idx + 1].start() if idx + 1 < len(num_blocks) else len(sec_body)
                    block = sec_body[b_start:b_end]

                    focus_points: list[str] = []
                    focus_match = re.search(r"\*?Focus:\*?[ \t]*(.+)", block, re.IGNORECASE)
                    if focus_match:
                        raw_focus = _strip_markdown_wrapping(focus_match.group(1))
                        focus_points = [f.strip() for f in raw_focus.split(",") if f.strip()]

                    q_prompt = ""
                    if _looks_like_question_or_prompt(inline_rest):
                        q_prompt = inline_rest
                    else:
                        for line in block.splitlines():
                            cand = re.sub(r"^\s*[-*]\s*", "", line).strip()
                            if cand.lower().startswith(("*focus:", "focus:")):
                                continue
                            if _looks_like_question_or_prompt(cand):
                                q_prompt = _strip_markdown_wrapping(cand)
                                break
                    if q_prompt:
                        _make_tech_question(screening_qs, q_prompt, topic, focus_points)
            else:
                for line in sec_body.splitlines():
                    cand = re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", line.strip())
                    if _looks_like_question_or_prompt(cand):
                        _make_tech_question(screening_qs, cand, "Technical Screening")

        # 2. Extract flagged gaps from Skills Match & Gap Matrix table
        gap_topics: list[tuple[str, str]] = []
        for line in markdown.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|") or stripped.startswith("| :---") or stripped.startswith("| Technology"):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) >= 3:
                tech_name = cells[0]
                profile_status = cells[2].lower()
                priority = cells[3] if len(cells) >= 4 else "High Priority Revision"
                if any(marker in profile_status for marker in ("gap", "missing", "unfamiliar", "partial")) or (
                    "high priority" in priority.lower()
                ):
                    gap_topics.append((tech_name, priority))

        for tech_name, priority in gap_topics:
            # Synthesize a targeted gap-probing question for each flagged skill gap
            _make_tech_question(
                gap_qs,
                f"The technical gap analysis for {role} at {company} highlights {tech_name} ({priority}). "
                f"Walk me through your hands-on experience with {tech_name}, its core architecture and failure modes, "
                f"and how you would apply it in a production {role} environment.",
                topic=f"Skills Gap: {tech_name}",
                focus_points=[tech_name, priority, "architecture", "trade-offs", "production reliability"],
            )

        # 3. Extract study focus areas if additional technical questions are useful
        study_sec = re.search(
            r"^#{1,3}\s+.*?Prioritized Study Focus.*$",
            markdown,
            re.IGNORECASE | re.MULTILINE,
        )
        if study_sec:
            s_start = study_sec.end()
            next_h2 = re.search(r"\n#{1,2}\s+", markdown[s_start:])
            s_body = markdown[s_start : s_start + next_h2.start()] if next_h2 else markdown[s_start:]
            for m in re.finditer(r"^[ \t]*[-*][ \t]+\*\*([^*:]+):?\*\*:?[ \t]*(.+)$", s_body, re.MULTILINE):
                area = _strip_markdown_wrapping(m.group(1)).rstrip(":")
                detail = _strip_markdown_wrapping(m.group(2))
                _make_tech_question(
                    study_qs,
                    f"Focusing on {area}: {detail} Tell me about a concrete system where you applied "
                    f"or evaluated these principles and the engineering trade-offs you made.",
                    topic=area,
                    focus_points=[area, detail],
                )

        # Interleave explicit screening questions and skill-gap probing questions
        questions: list[InterviewQuestion] = []
        s_idx = 0
        g_idx = 0
        while s_idx < len(screening_qs) or g_idx < len(gap_qs):
            if s_idx < len(screening_qs):
                questions.append(screening_qs[s_idx])
                s_idx += 1
            if g_idx < len(gap_qs):
                questions.append(gap_qs[g_idx])
                g_idx += 1
        questions.extend(study_qs)

        return questions

    def _build_jd_fallback_questions(
        self,
        *,
        company: str,
        role: str,
        raw_jd_md: str,
        category: Literal["behavioral", "technical"],
    ) -> list[InterviewQuestion]:
        """Generate sensible fallback questions from raw_job_description.md when prep docs are sparse."""
        lowered_jd = raw_jd_md.lower()
        found_tech = [
            kw for kw in _COMMON_TECH_TERMS if re.search(r"\b" + re.escape(kw) + r"\b", lowered_jd)
        ]

        if category == "behavioral":
            return [
                InterviewQuestion(
                    question=f"Tell me about a high-impact technical project you led that prepares you for the {role} role at {company}.",
                    category="behavioral",
                    topic="Ownership & Impact",
                    source_doc=RAW_JD_FILENAME if raw_jd_md else "fallback",
                    focus_points=["ownership", "architecture", "quantified impact"],
                ),
                InterviewQuestion(
                    question="Describe a time you diagnosed and resolved a critical production bottleneck or reliability issue under pressure.",
                    category="behavioral",
                    topic="Troubleshooting & Resilience",
                    source_doc=RAW_JD_FILENAME if raw_jd_md else "fallback",
                    focus_points=["root cause analysis", "observability", "prevention"],
                ),
                InterviewQuestion(
                    question="Tell me about a technical disagreement with a peer or stakeholder and how you reached alignment.",
                    category="behavioral",
                    topic="Collaboration & Communication",
                    source_doc=RAW_JD_FILENAME if raw_jd_md else "fallback",
                    focus_points=["data-driven trade-offs", "consensus", "team impact"],
                ),
            ]

        tech_label = ", ".join(t.capitalize() for t in found_tech[:4]) or "distributed backend services"
        return [
            InterviewQuestion(
                question=f"For the {role} position at {company}, how would you design a scalable, fault-tolerant service using {tech_label}?",
                category="technical",
                topic=f"System Design ({tech_label})",
                source_doc=RAW_JD_FILENAME if raw_jd_md else "fallback",
                focus_points=found_tech[:4] or ["scalability", "reliability", "trade-offs"],
            ),
            InterviewQuestion(
                question="Describe how you profile performance bottlenecks, optimize storage/queries, and validate latency improvements.",
                category="technical",
                topic="Performance & Storage",
                source_doc=RAW_JD_FILENAME if raw_jd_md else "fallback",
                focus_points=["profiling", "caching", "indexing", "p95 latency"],
            ),
        ]

    def _extract_questions_with_gemini(
        self,
        context: dict[str, Any],
        max_questions: int,
        mode: Literal["mixed", "behavioral", "technical"],
        client: Any,
    ) -> list[InterviewQuestion]:
        """Use GeminiClient.generate_structured to extract targeted interview questions."""
        system_instruction = (
            "You are a senior technical interviewer preparing a candidate for a mock interview. "
            "Extract targeted, realistic interview questions grounded strictly in the supplied prep "
            "documents and job description. Do not invent fake candidate background."
        )
        prompt = (
            f"Extract up to {max_questions} targeted interview questions for:\n"
            f"Company: {context['company']}\n"
            f"Role: {context['role']}\n"
            f"Requested Mode: {mode} ('mixed' = balanced behavioral + technical gap questions; "
            "'behavioral' = only behavioral; 'technical' = only technical gap/architecture).\n\n"
            f"TARGETED BEHAVIORAL QA:\n{context['targeted_behavioral_qa'][:7000]}\n\n"
            f"TECHNICAL SKILLS GAP:\n{context['technical_skills_gap'][:7000]}\n\n"
            f"RAW JOB DESCRIPTION:\n{context['raw_job_description'][:4000]}"
        )
        parsed = client.generate_structured(
            prompt=prompt,
            response_schema=_ExtractedQuestionsBatch,
            system_instruction=system_instruction,
            temperature=0.2,
        )
        raw_questions = getattr(parsed, "questions", None)
        if not isinstance(raw_questions, list):
            return []
        return [q for q in raw_questions if isinstance(q, InterviewQuestion) and q.question.strip()]

    def start_session(
        self,
        dossier_dir_or_slug: Path | str,
        max_questions: int = 5,
        mode: Literal["mixed", "behavioral", "technical"] | str = "mixed",
        gemini_client: GeminiClient | None = None,
    ) -> PracticeSessionState:
        """Initialize an interactive interview practice session from a company dossier.

        Args:
            dossier_dir_or_slug: Direct dossier directory Path or company/slug identifier.
            max_questions: Maximum number of questions to include in the session (>= 1).
            mode: Practice mode ('mixed', 'behavioral', or 'technical').
            gemini_client: Optional GeminiClient override for LLM question extraction.

        Returns:
            Initialized PracticeSessionState ready for sequential questioning.
        """
        if max_questions < 1:
            raise ValueError("max_questions must be at least 1.")

        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in SUPPORTED_MODES:
            raise ValueError(f"Unsupported mode '{mode}'. Expected one of: {', '.join(SUPPORTED_MODES)}.")

        context = self.load_context(dossier_dir_or_slug)
        client_to_use = gemini_client if gemini_client is not None else self.gemini_client

        behavioral_qs: list[InterviewQuestion] = []
        technical_qs: list[InterviewQuestion] = []

        if client_to_use is not None and hasattr(client_to_use, "generate_structured"):
            try:
                llm_qs = self._extract_questions_with_gemini(
                    context=context,
                    max_questions=max_questions,
                    mode=normalized_mode,  # type: ignore[arg-type]
                    client=client_to_use,
                )
                for q in llm_qs:
                    if q.category == "technical":
                        technical_qs.append(q)
                    else:
                        behavioral_qs.append(q)
            except Exception as exc:
                logger.warning("Gemini question extraction failed (%s); using offline markdown parser.", exc)
                behavioral_qs = []
                technical_qs = []

        # Parse offline markdown docs if needed to fulfill requested mode/count
        if normalized_mode in ("mixed", "behavioral") and len(behavioral_qs) < max_questions:
            parsed_b = self._parse_behavioral_questions(context["targeted_behavioral_qa"])
            seen_b = {q.question.lower() for q in behavioral_qs}
            for q in parsed_b:
                if q.question.lower() not in seen_b:
                    behavioral_qs.append(q)
                    seen_b.add(q.question.lower())
            if not behavioral_qs:
                behavioral_qs.extend(
                    self._build_jd_fallback_questions(
                        company=context["company"],
                        role=context["role"],
                        raw_jd_md=context["raw_job_description"],
                        category="behavioral",
                    )
                )

        if normalized_mode in ("mixed", "technical") and len(technical_qs) < max_questions:
            parsed_t = self._parse_technical_questions(
                context["technical_skills_gap"],
                company=context["company"],
                role=context["role"],
            )
            seen_t = {q.question.lower() for q in technical_qs}
            for q in parsed_t:
                if q.question.lower() not in seen_t:
                    technical_qs.append(q)
                    seen_t.add(q.question.lower())
            if not technical_qs:
                technical_qs.extend(
                    self._build_jd_fallback_questions(
                        company=context["company"],
                        role=context["role"],
                        raw_jd_md=context["raw_job_description"],
                        category="technical",
                    )
                )

        selected: list[InterviewQuestion] = []
        if normalized_mode == "behavioral":
            selected = behavioral_qs[:max_questions]
        elif normalized_mode == "technical":
            selected = technical_qs[:max_questions]
        else:
            # Interleave behavioral and technical questions for a balanced mixed interview
            b_idx = 0
            t_idx = 0
            while len(selected) < max_questions and (b_idx < len(behavioral_qs) or t_idx < len(technical_qs)):
                if b_idx < len(behavioral_qs):
                    selected.append(behavioral_qs[b_idx])
                    b_idx += 1
                if len(selected) < max_questions and t_idx < len(technical_qs):
                    selected.append(technical_qs[t_idx])
                    t_idx += 1

        numbered_questions: list[InterviewQuestion] = []
        for idx, q in enumerate(selected, start=1):
            numbered_questions.append(q.model_copy(update={"id": f"q{idx}"}))

        gap_summary_lines = [
            line.strip()
            for line in context["technical_skills_gap"].splitlines()
            if "|" in line and ("gap" in line.lower() or "high priority" in line.lower())
        ]

        return PracticeSessionState(
            company=context["company"],
            role=context["role"],
            role_category=context["role_category"],
            dossier_dir=str(context["dossier_dir"]),
            mode=normalized_mode,  # type: ignore[arg-type]
            max_questions=max_questions,
            questions=numbered_questions,
            current_index=0,
            current_question=None,
            completed=len(numbered_questions) == 0,
            job_description_excerpt=context["raw_job_description"][:1000],
            skills_gap_summary="\n".join(gap_summary_lines[:8]),
        )

    def get_next_question(self, session: PracticeSessionState) -> InterviewQuestion | None:
        """Return the next unasked interview question sequentially, or None if exhausted."""
        if session.current_index >= len(session.questions):
            session.current_question = None
            session.completed = True
            return None

        question = session.questions[session.current_index]
        session.current_index += 1
        session.current_question = question
        return question

    # ------------------------------------------------------------------
    # Candidate Profile Evidence Search
    # ------------------------------------------------------------------
    def _collect_profile_evidence_records(self) -> list[dict[str, Any]]:
        """Build searchable evidence records from story_bank, experience, and projects."""
        records: list[dict[str, Any]] = []

        for story in self.story_bank:
            story_id = str(story.get("id", "")).strip()
            title = str(story.get("title", "STAR Story")).strip()
            competencies = story.get("competencies", [])
            comp_list = [str(c).strip() for c in competencies] if isinstance(competencies, list) else []
            situation = str(story.get("situation", "")).strip()
            task = str(story.get("task", "")).strip()
            action = str(story.get("action", "")).strip()
            result = str(story.get("result", "")).strip()
            metrics = story.get("metrics", [])
            metrics_list = [str(m).strip() for m in metrics] if isinstance(metrics, list) else []

            full_text = " ".join([story_id, title, " ".join(comp_list), situation, task, action, result, " ".join(metrics_list)])
            summary_parts: list[str] = [f"[Story: {title}" + (f" ({story_id})" if story_id else "") + "]"]
            if metrics_list:
                summary_parts.append("Metrics: " + "; ".join(metrics_list[:2]) + ".")
            if action:
                summary_parts.append("Action: " + action[:160].rstrip(".") + ".")
            elif result:
                summary_parts.append("Result: " + result[:160].rstrip(".") + ".")

            records.append(
                {
                    "kind": "story_bank",
                    "id": story_id,
                    "title": title,
                    "search_text": full_text,
                    "has_metrics": bool(metrics_list or _extract_metrics_snippets(full_text)),
                    "formatted": " ".join(summary_parts),
                }
            )

        for exp in self.experience:
            company = str(exp.get("company", "")).strip()
            position = str(exp.get("position", exp.get("title", "Engineer"))).strip()
            start_d = str(exp.get("start_date", "")).strip()
            end_d = str(exp.get("end_date", "")).strip()
            date_span = f" ({start_d} to {end_d})" if start_d or end_d else ""
            highlights = exp.get("highlights", [])
            if not isinstance(highlights, list):
                highlights = []

            for hl in highlights:
                hl_str = str(hl).strip()
                if not hl_str:
                    continue
                full_text = f"{company} {position} {hl_str}"
                records.append(
                    {
                        "kind": "experience",
                        "id": company.lower(),
                        "title": f"{position} at {company}",
                        "search_text": full_text,
                        "has_metrics": bool(_extract_metrics_snippets(hl_str)),
                        "formatted": f"[Experience: {position} at {company}{date_span}] {hl_str}",
                    }
                )

        for proj in self.projects:
            name = str(proj.get("name", "Project")).strip()
            highlights = proj.get("highlights", [])
            if not isinstance(highlights, list):
                highlights = []

            for hl in highlights:
                hl_str = str(hl).strip()
                if not hl_str:
                    continue
                full_text = f"{name} {hl_str}"
                records.append(
                    {
                        "kind": "projects",
                        "id": name.lower(),
                        "title": name,
                        "search_text": full_text,
                        "has_metrics": bool(_extract_metrics_snippets(hl_str)),
                        "formatted": f"[Project: {name}] {hl_str}",
                    }
                )

        return records

    def find_relevant_evidence(
        self,
        query_text: str,
        *,
        topic: str = "",
        mapped_story_id: str = "",
        limit: int = 4,
    ) -> list[str]:
        """Search story_bank, experience, and projects for real grounded evidence."""
        records = self._collect_profile_evidence_records()
        if not records:
            return []

        query_tokens = _tokenize(f"{query_text} {topic}")
        mapped_lower = mapped_story_id.strip().lower()

        scored: list[tuple[float, str]] = []
        seen_formatted: set[str] = set()

        for rec in records:
            rec_tokens = _tokenize(rec["search_text"])
            overlap = len(query_tokens & rec_tokens)

            score = float(overlap) * 2.0
            if rec["kind"] == "story_bank":
                score += 1.5
            if rec["has_metrics"]:
                score += 1.25
            if mapped_lower and (
                mapped_lower == rec["id"].lower()
                or mapped_lower in rec["title"].lower()
                or rec["title"].lower() in mapped_lower
            ):
                score += 5.0

            formatted = rec["formatted"]
            if formatted not in seen_formatted:
                seen_formatted.add(formatted)
                scored.append((score, formatted))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:limit]]

    # ------------------------------------------------------------------
    # STAR Evaluation (Offline Rubric + Gemini)
    # ------------------------------------------------------------------
    def _evaluate_star_offline(
        self,
        question: InterviewQuestion,
        answer_text: str,
        suggested_evidence: list[str],
    ) -> STAREvaluation:
        """Perform deterministic STAR rubric scoring and coaching without an LLM."""
        cleaned = answer_text.strip()
        words = re.findall(r"\b[\w'-]+\b", cleaned)
        word_count = len(words)
        lowered = cleaned.lower()

        if word_count < 6:
            return STAREvaluation(
                question_id=question.id,
                question_text=question.question,
                answer_text=cleaned,
                situation_score=1,
                task_score=1,
                action_score=1,
                result_score=1,
                overall_score=1.0,
                strengths=[],
                gaps=[
                    "Response is too brief to demonstrate Situation, Task, Action, or Result.",
                    "Missing concrete technical steps and measurable impact.",
                ],
                coaching_feedback=(
                    "Expand your response into a full 90-second STAR narrative: set the technical "
                    "context and stakes (Situation), state your ownership goal (Task), detail the "
                    "exact actions and architectural decisions you took (Action), and close with "
                    "quantified outcomes (Result)."
                ),
                suggested_evidence=suggested_evidence,
                evaluation_mode="offline",
            )

        # 1. Situation score
        sit_context_hits = len(
            re.findall(
                r"\b(?:situation|context|at\s+[a-z0-9_]+|when\s+i|during|while\s+working|"
                r"on\s+my\s+team|in\s+production|previous\s+role|legacy|system|service|project|audit)\b",
                lowered,
            )
        )
        sit_stakes_hits = len(
            re.findall(
                r"\b(?:bottleneck|timeout|spike|latency|failure|regression|outage|dropped|"
                r"challenge|issue|problem|slow|burst|error|risk|constraint|ambiguous|conflict|disagree)\b",
                lowered,
            )
        )
        has_explicit_sit = bool(re.search(r"\bsituation\s*:", lowered))
        sit_points = (2 if sit_context_hits >= 1 else 0) + (1 if sit_context_hits >= 2 else 0) + (
            1 if sit_stakes_hits >= 1 else 0
        ) + (1 if (has_explicit_sit or (sit_stakes_hits >= 2 and word_count >= 25)) else 0)
        situation_score = max(1, min(5, 1 + sit_points if sit_context_hits or sit_stakes_hits else 1))
        if situation_score == 1 and word_count >= 30:
            situation_score = 2

        # 2. Task score
        task_hits = len(
            re.findall(
                r"\b(?:my\s+(?:task|goal|role|responsibility|objective)|task\s*:|goal\s+was|"
                r"i\s+(?:needed\s+to|had\s+to|was\s+tasked|was\s+responsible|owned|aimed\s+to|set\s+out\s+to)|"
                r"needed\s+to|had\s+to|objective|requirement|target|without\s+(?:downtime|breaking)|zero\s+downtime)\b",
                lowered,
            )
        )
        has_explicit_task = bool(re.search(r"\btask\s*:", lowered))
        if has_explicit_task or task_hits >= 2:
            task_score = 5 if word_count >= 35 else 4
        elif task_hits == 1:
            task_score = 4 if sit_stakes_hits >= 1 else 3
        elif re.search(r"\bto\s+(?:improve|reduce|eliminate|scale|upgrade|design|establish|reach)\b", lowered):
            task_score = 3
        elif word_count >= 25:
            task_score = 2
        else:
            task_score = 1

        # 3. Action score
        first_person_actions = len(
            re.findall(
                r"\bi\s+(?:architected|designed|built|implemented|profiled|refactored|created|"
                r"developed|migrated|introduced|configured|benchmarked|automated|isolated|"
                r"wrote|rolled\s+out|led|coordinated|partnered|analyzed|debugged|resolved|"
                r"optimized|tested|deployed|decoupled|evaluated|proposed|instrumented)\b",
                lowered,
            )
        )
        general_action_verbs = len(
            re.findall(
                r"\b(?:architected|designed|built|implemented|profiled|refactored|decoupled|"
                r"migrated|introduced|benchmarked|automated|isolated|rolled\s+out|optimized|"
                r"indexed|cached|configured|deployed|tested|instrumented|sharded|parallelized)\b",
                lowered,
            )
        )
        tech_mentions = [
            kw for kw in _COMMON_TECH_TERMS if re.search(r"\b" + re.escape(kw) + r"\b", lowered)
        ]
        i_count = len(re.findall(r"\bi\b", lowered))
        we_count = len(re.findall(r"\bwe\b", lowered))
        has_explicit_action = bool(re.search(r"\baction\s*:", lowered))

        action_score = 1
        if first_person_actions >= 2 and (len(tech_mentions) >= 2 or general_action_verbs >= 3):
            action_score = 5
        elif first_person_actions >= 1 and (len(tech_mentions) >= 1 or general_action_verbs >= 2):
            action_score = 4
        elif has_explicit_action or general_action_verbs >= 2 or (first_person_actions >= 1):
            action_score = 3
        elif general_action_verbs >= 1 or word_count >= 20:
            action_score = 2

        # Penalize plural-only ("we") narratives lacking individual ownership ("I")
        we_only_penalty = we_count >= 2 and i_count == 0
        if we_only_penalty and action_score > 3:
            action_score = 3

        # Check technical focus-point alignment for technical questions
        focus_tokens = _tokenize(" ".join(question.focus_points) + " " + question.topic)
        answer_tokens = _tokenize(cleaned)
        matched_focus = focus_tokens & answer_tokens
        if question.category == "technical" and len(tech_mentions) == 0 and not matched_focus and action_score > 3:
            action_score = 3

        # 4. Result score
        metrics_found = _extract_metrics_snippets(cleaned)
        outcome_verbs = len(
            re.findall(
                r"\b(?:result\s*:|as\s+a\s+result|resulting\s+in|reduced|improved|increased|cut|"
                r"eliminated|saved|achieved|passed|adopted|stabilized|delivered|shipped|"
                r"caught|lowered|prevented|outcome|impact|lesson|takeaway)\b",
                lowered,
            )
        )
        if len(metrics_found) >= 2 and outcome_verbs >= 1:
            result_score = 5
        elif len(metrics_found) >= 1 and outcome_verbs >= 1:
            result_score = 4
        elif len(metrics_found) >= 1 or outcome_verbs >= 2:
            result_score = 3
        elif outcome_verbs == 1 or re.search(r"\b(?:worked|finished|completed|successful|better|faster)\b", lowered):
            result_score = 2
        else:
            result_score = 1

        overall_score = round(
            (situation_score + task_score + action_score + result_score) / 4.0,
            2,
        )

        # Build Strengths
        strengths: list[str] = []
        if situation_score >= 4:
            strengths.append("Clearly establishes technical context, stakes, and baseline problem constraints.")
        if task_score >= 4:
            strengths.append("Defines personal responsibility and target engineering objective cleanly.")
        if action_score >= 4:
            strengths.append("Demonstrates strong first-person ownership and concrete technical execution.")
        if tech_mentions:
            strengths.append(
                f"References specific technologies/mechanisms ({', '.join(tech_mentions[:4])})."
            )
        if result_score >= 4 and metrics_found:
            strengths.append(f"Quantifies impact with concrete metrics ({', '.join(metrics_found[:3])}).")
        elif result_score >= 3 and outcome_verbs >= 1:
            strengths.append("Articulates a clear positive engineering or business outcome.")

        if not strengths and word_count >= 12:
            strengths.append("Addresses the prompt directly with relevant background.")

        # Build Gaps
        gaps: list[str] = []
        if situation_score <= 2:
            gaps.append("Situation is underspecified: clarify the system scale, baseline pain point, and stakes.")
        if task_score <= 2:
            gaps.append("Task/ownership is unclear: explicitly state what goal or constraint you personally owned.")
        if we_only_penalty:
            gaps.append("Uses 'we' without specifying your personal 'I' contributions and technical decisions.")
        elif action_score <= 2:
            gaps.append("Action lacks technical depth: walk through step-by-step architecture, trade-offs, and implementation.")
        if result_score <= 2:
            gaps.append("Missing concrete Result: state the measurable engineering or business impact.")
        elif result_score == 3 and not metrics_found:
            gaps.append("Result lacks hard metrics: add before/after numbers (e.g., latency %, error reduction, throughput).")

        if question.category == "technical" and focus_tokens and len(matched_focus) == 0:
            focus_preview = ", ".join(question.focus_points[:3]) or question.topic
            gaps.append(
                f"Technical prompt alignment: explicitly discuss core focus concepts ({focus_preview})."
            )

        # Build Coaching Feedback
        if overall_score >= 4.25 and not gaps:
            coaching_feedback = (
                "Strong STAR structure with clear ownership, technical rigor, and quantified impact. "
                "Keep delivery crisp (90-120 seconds) and prepare to field follow-up questions on trade-offs."
            )
        else:
            primary_gap = gaps[0] if gaps else "Sharpen your technical trade-offs and quantified outcomes."
            coaching_parts = [f"Priority improvement: {primary_gap}"]
            if suggested_evidence:
                coaching_parts.append(
                    f"Consider anchoring your answer with verified profile evidence such as {suggested_evidence[0]}."
                )
            coaching_feedback = " ".join(coaching_parts)

        return STAREvaluation(
            question_id=question.id,
            question_text=question.question,
            answer_text=cleaned,
            situation_score=situation_score,
            task_score=task_score,
            action_score=action_score,
            result_score=result_score,
            overall_score=overall_score,
            strengths=strengths,
            gaps=gaps,
            coaching_feedback=coaching_feedback,
            suggested_evidence=suggested_evidence,
            evaluation_mode="offline",
        )

    def _evaluate_star_with_gemini(
        self,
        session: PracticeSessionState,
        question: InterviewQuestion,
        answer_text: str,
        verified_evidence: list[str],
        client: Any,
    ) -> STAREvaluation:
        """Evaluate an interview answer using GeminiClient.generate_structured."""
        evidence_block = "\n".join(f"- {item}" for item in verified_evidence) or "- (No matching profile records)"
        focus_str = ", ".join(question.focus_points) if question.focus_points else question.topic

        system_instruction = (
            "You are an executive technical interview coach evaluating a candidate's answer using "
            "the STAR framework (Situation, Task, Action, Result).\n"
            "CRITICAL RULES:\n"
            "1. Score situation_score, task_score, action_score, and result_score strictly from 1 to 5.\n"
            "2. Compute overall_score from 1.0 to 5.0.\n"
            "3. Provide concise, actionable strengths, gaps, and coaching_feedback.\n"
            "4. For suggested_evidence, cite ONLY real stories, projects, experiences, or metrics from "
            "the VERIFIED CANDIDATE PROFILE EVIDENCE list below. Never fabricate companies, stories, or numbers."
        )
        prompt = (
            f"TARGET ROLE: {session.role} at {session.company}\n"
            f"QUESTION ({question.category}): {question.question}\n"
            f"TOPIC / FOCUS AREAS: {focus_str}\n\n"
            f"CANDIDATE ANSWER:\n{answer_text.strip()}\n\n"
            f"VERIFIED CANDIDATE PROFILE EVIDENCE:\n{evidence_block}\n\n"
            "Return the structured STAREvaluation JSON."
        )

        result = client.generate_structured(
            prompt=prompt,
            response_schema=STAREvaluation,
            system_instruction=system_instruction,
            temperature=0.2,
        )
        if isinstance(result, dict):
            result = STAREvaluation.model_validate(result)

        # Ensure suggested_evidence remains strictly grounded in real profile entries
        final_evidence = list(result.suggested_evidence) if result.suggested_evidence else []
        if not final_evidence:
            final_evidence = verified_evidence
        else:
            records = self._collect_profile_evidence_records()
            known_corpus = " ".join(rec["search_text"].lower() for rec in records)
            grounded_items: list[str] = []
            for item in final_evidence:
                item_tokens = _tokenize(item)
                item_metrics = _extract_metrics_snippets(item)
                metrics_ok = all(m.lower() in known_corpus for m in item_metrics)
                record_match = any(
                    len(item_tokens & _tokenize(rec["search_text"])) >= max(2, len(item_tokens) // 2)
                    or (rec["id"] and rec["id"].lower() in item.lower())
                    for rec in records
                )
                if item_tokens and metrics_ok and record_match:
                    grounded_items.append(item)
            final_evidence = grounded_items or verified_evidence

        return result.model_copy(
            update={
                "question_id": question.id,
                "question_text": question.question,
                "answer_text": answer_text.strip(),
                "suggested_evidence": final_evidence,
                "evaluation_mode": "gemini",
            }
        )

    def submit_answer(
        self,
        session: PracticeSessionState,
        answer_text: str,
        gemini_client: GeminiClient | None = None,
        *,
        question: InterviewQuestion | None = None,
    ) -> STAREvaluation:
        """Evaluate the candidate's answer against the active STAR question and record the turn.

        Args:
            session: Active PracticeSessionState instance.
            answer_text: Candidate's spoken/written answer to the current question.
            gemini_client: Optional GeminiClient override for structured LLM STAR scoring.
            question: Optional explicit InterviewQuestion override.

        Returns:
            STAREvaluation containing 1-5 STAR scores, strengths, gaps, coaching feedback,
            and real profile-grounded suggested_evidence.
        """
        if not isinstance(answer_text, str):
            raise ValueError("answer_text must be a string.")

        target_question = question
        if target_question is None:
            if session.current_question is not None:
                target_question = session.current_question
            elif session.current_index < len(session.questions):
                target_question = session.questions[session.current_index]
                session.current_index += 1
            elif not session.questions:
                raise ValueError("Practice session has no questions to evaluate.")
            else:
                raise ValueError("All questions in this practice session have already been answered.")

        suggested_evidence = self.find_relevant_evidence(
            query_text=f"{target_question.question} {answer_text}",
            topic=f"{target_question.topic} {' '.join(target_question.focus_points)}",
            mapped_story_id=target_question.mapped_story_id,
            limit=4,
        )

        client_to_use = gemini_client if gemini_client is not None else self.gemini_client
        evaluation: STAREvaluation | None = None

        if client_to_use is not None and hasattr(client_to_use, "generate_structured"):
            try:
                evaluation = self._evaluate_star_with_gemini(
                    session=session,
                    question=target_question,
                    answer_text=answer_text,
                    verified_evidence=suggested_evidence,
                    client=client_to_use,
                )
            except Exception as exc:
                logger.warning("Gemini STAR evaluation failed (%s); falling back to offline rubric.", exc)
                evaluation = None

        if evaluation is None:
            evaluation = self._evaluate_star_offline(
                question=target_question,
                answer_text=answer_text,
                suggested_evidence=suggested_evidence,
            )

        turn = PracticeTurn(
            question=target_question,
            answer=answer_text.strip(),
            evaluation=evaluation,
        )
        session.turns.append(turn)
        session.evaluations.append(evaluation)
        session.current_question = None
        if session.current_index >= len(session.questions):
            session.completed = True

        return evaluation

    def get_session_summary(self, session: PracticeSessionState) -> dict[str, Any]:
        """Compute aggregate STAR dimension averages and recurring gaps for a session."""
        evals = session.evaluations
        total_answered = len(evals)
        total_questions = len(session.questions)

        if total_answered == 0:
            return {
                "company": session.company,
                "role": session.role,
                "mode": session.mode,
                "questions_answered": 0,
                "total_questions": total_questions,
                "average_scores": {
                    "situation": 0.0,
                    "task": 0.0,
                    "action": 0.0,
                    "result": 0.0,
                    "overall": 0.0,
                },
                "top_gaps": [],
            }

        avg_s = round(sum(e.situation_score for e in evals) / total_answered, 2)
        avg_t = round(sum(e.task_score for e in evals) / total_answered, 2)
        avg_a = round(sum(e.action_score for e in evals) / total_answered, 2)
        avg_r = round(sum(e.result_score for e in evals) / total_answered, 2)
        avg_overall = round(sum(e.overall_score for e in evals) / total_answered, 2)

        unique_gaps: list[str] = []
        for e in evals:
            for g in e.gaps:
                if g not in unique_gaps:
                    unique_gaps.append(g)

        return {
            "company": session.company,
            "role": session.role,
            "mode": session.mode,
            "questions_answered": total_answered,
            "total_questions": total_questions,
            "average_scores": {
                "situation": avg_s,
                "task": avg_t,
                "action": avg_a,
                "result": avg_r,
                "overall": avg_overall,
            },
            "top_gaps": unique_gaps[:5],
        }


def run_cli_session(
    company_or_dossier: str | Path,
    *,
    max_questions: int = 5,
    mode: Literal["mixed", "behavioral", "technical"] = "mixed",
    history_root: Path | str | None = None,
    master_profile_path: Path | str | None = None,
    use_gemini: bool = False,
    gemini_client: GeminiClient | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> PracticeSessionState:
    """Run an interactive terminal interview practice loop for a target company dossier."""
    resolved_client: Any = gemini_client
    if use_gemini and resolved_client is None:
        try:
            resolved_client = GeminiClient()
        except Exception as exc:
            output_fn(f"[Note] Gemini unavailable ({exc}); running offline STAR evaluator.")
            resolved_client = None

    agent = InterviewPracticeAgent(
        history_root=history_root,
        master_profile_path=master_profile_path,
        gemini_client=resolved_client,
    )
    session = agent.start_session(
        dossier_dir_or_slug=company_or_dossier,
        max_questions=max_questions,
        mode=mode,
        gemini_client=resolved_client,
    )

    output_fn("=" * 72)
    output_fn(f"JobApplier-Core Interview Practice | {session.company} - {session.role}")
    output_fn(f"Mode: {session.mode} | Questions: {len(session.questions)} | Dossier: {session.dossier_dir}")
    output_fn("Commands: type your STAR answer, 'skip' to skip, or 'quit' to finish early.")
    output_fn("=" * 72)

    total_q = len(session.questions)
    q_num = 0
    while (question := agent.get_next_question(session)) is not None:
        q_num += 1
        output_fn("")
        output_fn(f"[Question {q_num}/{total_q}] ({question.category.upper()}) {question.topic}")
        output_fn(f"  {question.question}")
        if question.focus_points:
            output_fn(f"  Focus hints: {', '.join(question.focus_points[:3])}")

        try:
            answer = input_fn("\nYour Answer> ")
        except (EOFError, KeyboardInterrupt):
            output_fn("\n[Session interrupted. Summarizing completed answers...]")
            break

        cmd = answer.strip().lower()
        if cmd in {"quit", "exit", ":q"}:
            output_fn("\n[Ending practice session early.]")
            break
        if cmd == "skip" or not answer.strip():
            session.current_question = None
            output_fn("[Skipped question.]")
            continue

        evaluation = agent.submit_answer(session, answer, gemini_client=resolved_client)

        output_fn("\n--- STAR Evaluation ---")
        output_fn(
            f"Scores: Situation {evaluation.situation_score}/5 | "
            f"Task {evaluation.task_score}/5 | "
            f"Action {evaluation.action_score}/5 | "
            f"Result {evaluation.result_score}/5 | "
            f"Overall {evaluation.overall_score:.2f}/5.0 ({evaluation.evaluation_mode})"
        )
        if evaluation.strengths:
            output_fn("Strengths:")
            for s in evaluation.strengths:
                output_fn(f"  + {s}")
        if evaluation.gaps:
            output_fn("Gaps:")
            for g in evaluation.gaps:
                output_fn(f"  - {g}")
        output_fn(f"Coaching: {evaluation.coaching_feedback}")
        if evaluation.suggested_evidence:
            output_fn("Suggested Profile Evidence:")
            for ev in evaluation.suggested_evidence:
                output_fn(f"  * {ev}")

    summary = agent.get_session_summary(session)
    avg = summary["average_scores"]
    output_fn("")
    output_fn("=" * 72)
    output_fn(
        f"Session Complete | Answered {summary['questions_answered']}/{summary['total_questions']} | "
        f"Avg Overall: {avg['overall']:.2f}/5.0"
    )
    if summary["questions_answered"] > 0:
        output_fn(
            f"STAR Averages -> S: {avg['situation']:.2f} | T: {avg['task']:.2f} | "
            f"A: {avg['action']:.2f} | R: {avg['result']:.2f}"
        )
    if summary["top_gaps"]:
        output_fn("Key Improvement Areas:")
        for g in summary["top_gaps"]:
            output_fn(f"  - {g}")
    output_fn("=" * 72)

    return session


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build command-line argument parser for interactive interview practice."""
    parser = argparse.ArgumentParser(
        prog="python -m src.agents.interview_practice",
        description="Interactive STAR interview practice simulator for JobApplier-Core.",
    )
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "--company",
        "-c",
        help="Company name or dossier folder slug to resolve inside the history directory.",
    )
    target_group.add_argument(
        "--dossier",
        "-d",
        help="Direct path to an application dossier directory.",
    )
    parser.add_argument(
        "--max-questions",
        "-n",
        type=int,
        default=5,
        help="Maximum number of practice questions (default: 5).",
    )
    parser.add_argument(
        "--mode",
        "-m",
        choices=SUPPORTED_MODES,
        default="mixed",
        help="Question mode: mixed, behavioral, or technical (default: mixed).",
    )
    parser.add_argument(
        "--history-root",
        default=None,
        help="Optional override for history root directory.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional override path for candidate master_profile.yaml.",
    )
    parser.add_argument(
        "--gemini",
        action="store_true",
        help="Enable GeminiClient for question extraction and STAR evaluation.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for interactive interview practice mode."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    target = args.dossier if args.dossier else args.company
    run_cli_session(
        target,
        max_questions=args.max_questions,
        mode=args.mode,
        history_root=args.history_root,
        master_profile_path=args.profile,
        use_gemini=args.gemini,
    )
    return 0


__all__ = [
    "InterviewPracticeAgent",
    "InterviewQuestion",
    "PracticeSessionState",
    "PracticeTurn",
    "STAREvaluation",
    "main",
    "run_cli_session",
]


if __name__ == "__main__":
    raise SystemExit(main())
