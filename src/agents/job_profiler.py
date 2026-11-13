"""Job Profiler and Requirement Extractor agent for JobApplier-Core."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Final, Literal

import requests
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field, field_validator

from src.engine.llm import GEMINI_FLASH, GeminiClient

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
RAW_JD_FILENAME: Final[str] = "raw_job_description.md"


class JobProfileSchema(BaseModel):
    """Structured representation of an analyzed job posting."""

    job_title: str = Field(
        ...,
        description="Official or normalized job title from the posting.",
    )
    company_name: str = Field(
        ...,
        description="Name of the hiring company or organization.",
    )
    location: str = Field(
        default="Not specified",
        description="Job location, city, state, country, or geographic restriction.",
    )
    work_type: str = Field(
        default="onsite",
        description="Work arrangement: 'remote', 'hybrid', or 'onsite'.",
    )
    role_category: Literal["full_time", "internships"] = Field(
        ...,
        description="Strict classification: 'full_time' for permanent/contract roles, or 'internships' for intern/co-op positions.",
    )
    required_skills: list[str] = Field(
        default_factory=list,
        description="Mandatory technical and domain skills explicitly requested in the posting.",
    )
    preferred_skills: list[str] = Field(
        default_factory=list,
        description="Nice-to-have, preferred, or bonus skills and qualifications.",
    )
    tech_stack: list[str] = Field(
        default_factory=list,
        description="Technologies, frameworks, programming languages, databases, or cloud tools mentioned.",
    )
    extracted_screening_questions: list[str] = Field(
        default_factory=list,
        description="Application screening questions or custom prompts extracted from the posting.",
    )
    raw_markdown: str = Field(
        default="",
        description="Raw or markdown formatted text of the job description.",
    )

    @field_validator("role_category", mode="before")
    @classmethod
    def _normalize_role_category(cls, value: Any) -> str:
        if not isinstance(value, str):
            return "full_time"
        cleaned = value.strip().lower()
        if any(term in cleaned for term in ["intern", "co-op", "coop", "student", "trainee"]):
            return "internships"
        return "full_time"

    @field_validator("work_type", mode="before")
    @classmethod
    def _normalize_work_type(cls, value: Any) -> str:
        if not isinstance(value, str):
            return "onsite"
        cleaned = value.strip().lower()
        if "remote" in cleaned:
            return "remote"
        if "hybrid" in cleaned:
            return "hybrid"
        return "onsite"

    @field_validator("location", mode="before")
    @classmethod
    def _normalize_location(cls, value: Any) -> str:
        if value is None:
            return "Not specified"
        cleaned = str(value).strip()
        return cleaned if cleaned else "Not specified"

    @field_validator("job_title", "company_name", mode="before")
    @classmethod
    def _normalize_non_empty_strings(cls, value: Any) -> str:
        if value is None:
            return "Not specified"
        cleaned = str(value).strip()
        return cleaned if cleaned else "Not specified"

    @field_validator(
        "required_skills",
        "preferred_skills",
        "tech_stack",
        "extracted_screening_questions",
        mode="before",
    )
    @classmethod
    def _normalize_string_lists(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            trimmed = value.strip()
            if not trimmed:
                return []
            return [part.strip() for part in trimmed.split(",") if part.strip()]
        if isinstance(value, (list, tuple, set)):
            result: list[str] = []
            for item in value:
                item_str = str(item).strip()
                if item_str:
                    result.append(item_str)
            return result
        return []

    def to_markdown(self) -> str:
        """Format this profile into a structured markdown briefing document."""
        return to_markdown_briefing(self)


def to_markdown_briefing(profile: JobProfileSchema) -> str:
    """Format a JobProfileSchema instance into a structured markdown briefing document."""
    lines: list[str] = [
        f"# Job Briefing: {profile.job_title} at {profile.company_name}",
        "",
        "## Position Overview",
        f"- **Company:** {profile.company_name}",
        f"- **Job Title:** {profile.job_title}",
        f"- **Role Category:** {profile.role_category}",
        f"- **Work Type:** {profile.work_type}",
        f"- **Location:** {profile.location}",
        "",
        "## Required Skills",
    ]
    if profile.required_skills:
        for skill in profile.required_skills:
            lines.append(f"- {skill}")
    else:
        lines.append("- None explicitly specified in posting")

    lines.extend(["", "## Preferred Skills"])
    if profile.preferred_skills:
        for skill in profile.preferred_skills:
            lines.append(f"- {skill}")
    else:
        lines.append("- None explicitly specified in posting")

    lines.extend(["", "## Tech Stack"])
    if profile.tech_stack:
        for tech in profile.tech_stack:
            lines.append(f"- {tech}")
    else:
        lines.append("- None explicitly specified in posting")

    lines.extend(["", "## Application Screening Questions"])
    if profile.extracted_screening_questions:
        for index, question in enumerate(profile.extracted_screening_questions, 1):
            lines.append(f"{index}. {question}")
    else:
        lines.append("- None detected in posting")

    if profile.raw_markdown and profile.raw_markdown.strip():
        lines.extend([
            "",
            "## Raw Job Description",
            profile.raw_markdown.strip(),
        ])

    lines.append("")
    return "\n".join(lines)


def save_raw_job_description(
    target_path: str | Path,
    content: str | JobProfileSchema,
) -> Path:
    """Save raw job description or profile markdown briefing to raw_job_description.md.

    Args:
        target_path: Destination directory path or specific destination file path.
        content: Either a JobProfileSchema (formatted to markdown) or raw string text.

    Returns:
        Path to the saved markdown file.
    """
    path = Path(target_path)
    if path.is_dir() or not path.suffix:
        destination = path / RAW_JD_FILENAME
    else:
        destination = path

    destination.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(content, JobProfileSchema):
        body = to_markdown_briefing(content)
    else:
        body = str(content)

    destination.write_text(body, encoding="utf-8")
    return destination


def clean_html_job_text(html: str) -> str:
    """Strip navigation, headers, footers, scripts, styles, and cookie banners from HTML."""
    soup = BeautifulSoup(html, "html.parser")

    # Decompose non-content and structural chrome tags
    chrome_tags = ["nav", "header", "footer", "script", "style", "noscript", "svg", "iframe"]
    for tag_name in chrome_tags:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Detect and decompose cookie banners, consent notices, and dialog modals
    def _is_banner_or_dialog(tag: Tag) -> bool:
        if not tag.name:
            return False
        tag_id = (tag.get("id") or "").lower()
        tag_class = " ".join(tag.get("class") or []).lower()
        tag_aria = (tag.get("aria-label") or "").lower()
        tag_role = (tag.get("role") or "").lower()

        keywords = (
            "cookie",
            "consent",
            "onetrust",
            "gdpr",
            "cookiebanner",
            "cookie-banner",
            "consent-banner",
            "privacypolicy",
            "usercentrics",
            "trustarc",
        )
        if any(kw in tag_id or kw in tag_class or kw in tag_aria for kw in keywords):
            return True
        if tag_role in ("dialog", "alertdialog") and any(
            kw in tag_id or kw in tag_class or kw in tag_aria for kw in ("cookie", "consent", "privacy")
        ):
            return True
        return False

    for banner in soup.find_all(_is_banner_or_dialog):
        banner.decompose()

    # Prefer main container if present and meaningful
    main_el = (
        soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find("article")
        or soup.body
        or soup
    )

    raw_text = main_el.get_text(separator="\n", strip=True)

    # Normalize excessive blank lines
    lines = [line.strip() for line in raw_text.splitlines()]
    cleaned_lines: list[str] = []
    prev_blank = False
    for line in lines:
        if not line:
            if not prev_blank:
                cleaned_lines.append("")
                prev_blank = True
        else:
            cleaned_lines.append(line)
            prev_blank = False

    return "\n".join(cleaned_lines).strip()


class JobProfilerAgent:
    """Agent responsible for extracting job postings from URLs or raw text and structuring requirements."""

    def __init__(
        self,
        gemini_client: GeminiClient | None = None,
        default_model: str = GEMINI_FLASH,
    ) -> None:
        """Initialize the JobProfilerAgent.

        Args:
            gemini_client: Optional pre-configured GeminiClient instance.
            default_model: Gemini model identifier to use for extraction.
        """
        self._gemini_client = gemini_client
        self.default_model = default_model

    @property
    def client(self) -> GeminiClient:
        """Lazy-initialize or return the GeminiClient instance."""
        if self._gemini_client is None:
            self._gemini_client = GeminiClient(default_model=self.default_model)
        return self._gemini_client

    def _fetch_via_playwright(self, url: str, timeout: float) -> str:
        """Fetch page HTML using Playwright Chromium headless."""
        from playwright.sync_api import sync_playwright

        def _execute() -> str:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    page = browser.new_page()
                    timeout_ms = int(timeout * 1000)
                    page.set_default_navigation_timeout(timeout_ms)
                    page.set_default_timeout(timeout_ms)
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

                    # In-browser DOM pruning of boilerplate elements
                    try:
                        page.evaluate(
                            """() => {
                                const selectors = [
                                    'nav', 'header', 'footer', 'script', 'style', 'noscript', 'svg', 'iframe',
                                    '[id*="cookie" i]', '[class*="cookie" i]',
                                    '[id*="consent" i]', '[class*="consent" i]',
                                    '[id*="onetrust" i]', '[class*="onetrust" i]'
                                ];
                                for (const selector of selectors) {
                                    document.querySelectorAll(selector).forEach(element => element.remove());
                                }
                            }"""
                        )
                    except Exception as eval_exc:
                        logger.debug("Playwright in-browser evaluate pruning skipped: %s", eval_exc)

                    return page.content()
                finally:
                    browser.close()

        # Handle execution when an asyncio event loop is already active
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(_execute).result()
        return _execute()

    def _fetch_via_fallback(self, url: str, timeout: float) -> str:
        """Fetch page HTML via requests or local file reading when Playwright fails."""
        if url.startswith("file://"):
            parsed_path = urllib.request.url2pathname(urllib.parse.urlparse(url).path)
            return Path(parsed_path).read_text(encoding="utf-8")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        response = requests.get(url, timeout=timeout, headers=headers)
        response.raise_for_status()
        return response.text

    def extract_text_from_url(self, url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
        """Extract clean job posting text from a URL using Playwright with BeautifulSoup fallback.

        Args:
            url: Job posting web URL (http://, https://, or file://).
            timeout: Timeout in seconds for page navigation and response.

        Returns:
            Extracted job posting text with boilerplate and banners removed.

        Raises:
            ValueError: If the URL scheme is unsupported.
            RuntimeError: If page fetching or text extraction fails completely.
        """
        if not isinstance(url, str) or not url.strip():
            raise ValueError("URL must be a non-empty string.")

        normalized_url = url.strip()
        if not normalized_url.startswith(("http://", "https://", "file://")):
            raise ValueError(
                f"Unsupported URL '{normalized_url}'. Expected http://, https://, or file:// prefix."
            )

        html_content: str | None = None
        playwright_error: Exception | None = None

        try:
            html_content = self._fetch_via_playwright(normalized_url, timeout=timeout)
        except Exception as exc:
            playwright_error = exc
            logger.warning(
                "Playwright retrieval failed for %s (%s). Attempting BeautifulSoup fallback.",
                normalized_url,
                exc,
            )

        if not html_content:
            try:
                html_content = self._fetch_via_fallback(normalized_url, timeout=timeout)
            except Exception as fallback_exc:
                raise RuntimeError(
                    f"Failed to extract text from URL '{normalized_url}' using Playwright and fallback: "
                    f"Playwright error: {playwright_error}; Fallback error: {fallback_exc}"
                ) from fallback_exc

        cleaned = clean_html_job_text(html_content)
        if not cleaned:
            raise RuntimeError(f"Text extraction from '{normalized_url}' resulted in empty content.")
        return cleaned

    def extract_from_text(
        self,
        raw_text: str,
        *,
        save_path: str | Path | None = None,
    ) -> JobProfileSchema:
        """Extract structured job profile information from raw job posting text using Gemini.

        Args:
            raw_text: Raw job description or posting text.
            save_path: Optional file path or directory where raw_job_description.md will be saved.

        Returns:
            Validated JobProfileSchema instance.

        Raises:
            ValueError: If raw_text is empty or blank.
        """
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ValueError("raw_text must be a non-empty string.")

        cleaned_input = raw_text.strip()

        system_instruction = (
            "You are an expert technical job description extractor and classification engine. "
            "Analyze the provided job posting text and extract structured information according to the schema.\n\n"
            "Strict Classification Rules:\n"
            "1. 'role_category': MUST be strictly either 'full_time' or 'internships'. "
            "Classify as 'internships' if the title or description specifies intern, internship, co-op, student worker, or summer associate. "
            "Otherwise classify as 'full_time' (including standard full-time, contract, part-time, or permanent positions).\n"
            "2. 'work_type': Classify as 'remote', 'hybrid', or 'onsite'.\n"
            "3. 'required_skills': List explicit mandatory technical and domain requirements.\n"
            "4. 'preferred_skills': List nice-to-have, preferred, or bonus qualifications.\n"
            "5. 'tech_stack': List programming languages, frameworks, databases, cloud platforms, and developer tools.\n"
            "6. 'extracted_screening_questions': Extract any application questions, questionnaire prompts, or required short-answer fields.\n"
            "Do not fabricate facts not present in the posting."
        )

        prompt = (
            "Parse and structure the following job posting into the requested JSON schema.\n\n"
            f"JOB POSTING TEXT:\n{cleaned_input}"
        )

        profile = self.client.generate_structured(
            prompt=prompt,
            response_schema=JobProfileSchema,
            model=self.default_model,
            system_instruction=system_instruction,
        )

        if not profile.raw_markdown or not profile.raw_markdown.strip():
            profile.raw_markdown = cleaned_input

        if save_path is not None:
            save_raw_job_description(save_path, profile)

        return profile

    def to_markdown_briefing(self, profile: JobProfileSchema) -> str:
        """Generate a formatted markdown briefing from a JobProfileSchema instance."""
        return to_markdown_briefing(profile)

    def save_raw_job_description(
        self,
        target_path: str | Path,
        content: str | JobProfileSchema,
    ) -> Path:
        """Save job description or briefing to raw_job_description.md at target path."""
        return save_raw_job_description(target_path, content)

    def profile_job(
        self,
        url_or_text: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        save_path: str | Path | None = None,
    ) -> tuple[JobProfileSchema, str]:
        """Profile a job posting from a URL or raw text string.

        Args:
            url_or_text: Web URL (http:// or https://) or raw job description text.
            timeout: Network timeout in seconds when fetching via URL.
            save_path: Optional destination file or directory path for raw_job_description.md.

        Returns:
            Tuple of (JobProfileSchema, markdown_briefing_string).
        """
        if not isinstance(url_or_text, str) or not url_or_text.strip():
            raise ValueError("url_or_text must be a non-empty string.")

        candidate = url_or_text.strip()
        if candidate.startswith(("http://", "https://", "file://")):
            text_to_parse = self.extract_text_from_url(candidate, timeout=timeout)
        else:
            text_to_parse = candidate

        profile = self.extract_from_text(text_to_parse, save_path=save_path)
        briefing = to_markdown_briefing(profile)
        return profile, briefing


def extract_text_from_url(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Extract clean text from a job posting URL."""
    agent = JobProfilerAgent()
    return agent.extract_text_from_url(url, timeout=timeout)


def profile_job(
    url_or_text: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    save_path: str | Path | None = None,
    gemini_client: GeminiClient | None = None,
) -> tuple[JobProfileSchema, str]:
    """Profile a job posting from a URL or raw text."""
    agent = JobProfilerAgent(gemini_client=gemini_client)
    return agent.profile_job(url_or_text, timeout=timeout, save_path=save_path)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "JobProfileSchema",
    "JobProfilerAgent",
    "RAW_JD_FILENAME",
    "clean_html_job_text",
    "extract_text_from_url",
    "profile_job",
    "save_raw_job_description",
    "to_markdown_briefing",
]
