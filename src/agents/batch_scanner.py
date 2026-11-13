"""Batch Intake and URL Liveness Checker for JobApplier-Core.

Verifies whether target job postings are still active, open, and accepting
applications prior to downstream LLM profiling, answer crafting, and browser automation.
Detects ATS portal types, expired job markers, HTTP error responses, and extracts
detected job titles.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Pattern

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    try:
        from playwright.sync_api import Page
    except (ImportError, ModuleNotFoundError):  # pragma: no cover
        Page = Any  # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 15.0
DEFAULT_MAX_CONCURRENCY: Final[int] = 5
DEFAULT_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Known ATS portal domains
PORTAL_DOMAINS: Final[dict[str, str]] = {
    "greenhouse.io": "greenhouse",
    "grnh.se": "greenhouse",
    "lever.co": "lever",
    "myworkdayjobs.com": "workday",
    "myworkday.com": "workday",
    "workday.com": "workday",
    "ashbyhq.com": "ashby",
    "smartrecruiters.com": "smartrecruiters",
    "jobvite.com": "jobvite",
    "taleo.net": "taleo",
    "icims.com": "icims",
    "breezy.hr": "breezy",
    "rippling.com": "rippling",
    "bamboohr.com": "bamboohr",
    "linkedin.com": "linkedin",
    "indeed.com": "indeed",
    "ziprecruiter.com": "ziprecruiter",
}

# Compiled regex patterns for detecting job expiration markers across ATS platforms
EXPIRATION_PATTERNS: Final[list[tuple[Pattern[str], str]]] = [
    # Explicit prompt phrases
    (re.compile(r"this job is no longer available", re.I), "This job is no longer available"),
    (re.compile(r"position has been filled", re.I), "Position has been filled"),
    (re.compile(r"the posting has closed", re.I), "The posting has closed"),
    (re.compile(r"\bjob is closed\b", re.I), "Job is closed"),
    (re.compile(r"application deadline passed", re.I), "Application deadline passed"),
    (re.compile(r"job posting not found", re.I), "Job posting not found"),
    (re.compile(r"this listing has expired", re.I), "This listing has expired"),

    # Lever expired notices
    (
        re.compile(r"the job you are looking for is no longer open", re.I),
        "The job you are looking for is no longer open",
    ),
    (
        re.compile(r"this posting is no longer accepting applications", re.I),
        "This posting is no longer accepting applications",
    ),
    (re.compile(r"this job posting is closed", re.I), "This job posting is closed"),
    (re.compile(r"this listing is closed", re.I), "This listing is closed"),
    (re.compile(r"this position is closed", re.I), "This position is closed"),

    # Greenhouse 404 / no-job notices
    (
        re.compile(r"the page you are looking for doesn['’]?t exist", re.I),
        "Greenhouse page does not exist",
    ),
    (
        re.compile(r"the job you are looking for has been filled or removed", re.I),
        "The job you are looking for has been filled or removed",
    ),
    (re.compile(r"we couldn['’]?t find that job", re.I), "Job not found"),
    (re.compile(r"\bno job found\b", re.I), "No job found"),

    # Workday inactive requisition notices
    (
        re.compile(
            r"requisition.*?(?:closed|no longer (?:active|open)|cannot be found|has expired)",
            re.I,
        ),
        "Workday inactive requisition",
    ),
    (re.compile(r"inactive requisition", re.I), "Inactive requisition"),
    (
        re.compile(r"the job posting you are looking for cannot be found", re.I),
        "The job posting you are looking for cannot be found",
    ),

    # General ATS closure and expiration phrases
    (
        re.compile(r"this position is no longer available", re.I),
        "This position is no longer available",
    ),
    (
        re.compile(r"this position is no longer accepting applications", re.I),
        "This position is no longer accepting applications",
    ),
    (
        re.compile(r"applications are no longer being accepted", re.I),
        "Applications are no longer being accepted",
    ),
    (
        re.compile(r"we are no longer accepting applications", re.I),
        "No longer accepting applications",
    ),
    (re.compile(r"this vacancy has expired", re.I), "This vacancy has expired"),
    (re.compile(r"this vacancy is closed", re.I), "This vacancy is closed"),
    (
        re.compile(r"this job posting has been archived", re.I),
        "This job posting has been archived",
    ),
    (re.compile(r"this job has expired", re.I), "This job has expired"),
    (re.compile(r"this position has expired", re.I), "This position has expired"),
    (re.compile(r"job opening has closed", re.I), "Job opening has closed"),
    (re.compile(r"this listing has been removed", re.I), "This listing has been removed"),
    (re.compile(r"posting has expired", re.I), "Posting has expired"),
    (re.compile(r"404.*?not found", re.I), "Page not found (404)"),
]


class JobLivenessResult(BaseModel):
    """Result of an individual job URL liveness inspection."""

    url: str = Field(..., description="Target job posting URL.")
    is_active: bool = Field(..., description="Whether the job posting is active and open.")
    status_code: int = Field(default=200, description="HTTP response status code.")
    portal_type: str = Field(default="unknown", description="Detected ATS or portal platform.")
    reason: str = Field(default="active", description="Explanation of status or expiration reason.")
    detected_title: str = Field(default="", description="Detected job title if page is active.")


class BatchScanSummary(BaseModel):
    """Summary of batch scan execution over a collection of job URLs."""

    total: int = Field(..., description="Total URLs processed.")
    active_count: int = Field(..., description="Count of confirmed active jobs.")
    inactive_count: int = Field(..., description="Count of inactive, expired, or invalid URLs.")
    results: list[JobLivenessResult] = Field(
        default_factory=list, description="Per-URL detailed liveness results."
    )


def detect_portal_type(url: str, html_content: str = "") -> str:
    """Identify ATS or portal platform from URL and optional HTML markup.

    Args:
        url: Job posting URL.
        html_content: Optional raw HTML markup.

    Returns:
        Portal identifier string (e.g. 'greenhouse', 'lever', 'workday', 'unknown').
    """
    if not isinstance(url, str):
        return "unknown"

    try:
        parsed = urllib.parse.urlparse(url.strip())
        netloc = (parsed.netloc or "").lower()
    except Exception:
        netloc = ""

    for domain_substr, portal_name in PORTAL_DOMAINS.items():
        if domain_substr in netloc:
            return portal_name

    # Check URL path / query markers
    clean_url_lower = url.lower()
    if "gh_jid=" in clean_url_lower or "gh_src=" in clean_url_lower:
        return "greenhouse"

    # Fallback to inspecting HTML markup signatures
    if html_content:
        html_lower = html_content[:20000].lower()
        if (
            "boards.greenhouse.io" in html_lower
            or "id=\"grnhse_app\"" in html_lower
            or "data-portal=\"greenhouse\"" in html_lower
        ):
            return "greenhouse"
        if "jobs.lever.co" in html_lower or "lever-job" in html_lower or "class=\"posting-headline\"" in html_lower:
            return "lever"
        if "myworkdayjobs" in html_lower or "data-automation-id=\"jobposting" in html_lower:
            return "workday"
        if "ashbyhq.com" in html_lower or "ashby" in html_lower:
            return "ashby"
        if "smartrecruiters.com" in html_lower:
            return "smartrecruiters"

    return "unknown"


def _extract_title_from_soup(soup: BeautifulSoup, portal_type: str = "unknown") -> str:
    """Extract job title from parsed BeautifulSoup HTML.

    Inspects JSON-LD Schema, portal-specific selectors, OpenGraph meta tags,
    headings, and title tag.

    Args:
        soup: BeautifulSoup parsed DOM.
        portal_type: Identified ATS platform name.

    Returns:
        Extracted and sanitized job title string, or empty string.
    """
    # 1. JSON-LD Structured Data
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw_json = script.string or ""
            if not raw_json.strip():
                continue
            data = json.loads(raw_json)
            items: list[Any]
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("@graph", [data]) if "@graph" in data else [data]
            else:
                items = []

            for item in items:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    candidate = item.get("title") or item.get("jobTitle")
                    if candidate and isinstance(candidate, str) and candidate.strip():
                        return candidate.strip()
        except Exception:
            continue

    # 2. Portal-specific selectors
    if portal_type == "greenhouse":
        el = soup.select_one("h1.app-title, #header h1, h1.job-title, .job-title")
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)

    if portal_type == "lever":
        el = soup.select_one(".posting-headline h2, .posting-header h2, h2.posting-headline")
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)

    if portal_type == "workday":
        el = soup.select_one(
            "[data-automation-id='jobPostingHeader'], "
            "[data-automation-id='jobPostingTitle'], "
            "h2[data-automation-id='jobPostingHeader']"
        )
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)

    if portal_type == "ashby":
        el = soup.select_one("h1._heading_1, h1[class*='heading'], h1")
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)

    if portal_type == "smartrecruiters":
        el = soup.select_one("h1.job-title, .job-detail-title")
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)

    # 3. Meta tags (OpenGraph / Twitter)
    og_meta = soup.find("meta", property="og:title") or soup.find(
        "meta", attrs={"name": "twitter:title"}
    )
    if og_meta:
        content = og_meta.get("content", "")
        if isinstance(content, str) and content.strip():
            cleaned = _clean_title_string(content.strip())
            if cleaned:
                return cleaned

    # 4. Standard semantic heading (first h1)
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        candidate = h1.get_text(strip=True)
        if len(candidate) > 2 and not any(
            kw in candidate.lower() for kw in ["cookies", "navigation", "error", "404"]
        ):
            return candidate

    # 5. HTML <title> tag fallback
    title_tag = soup.find("title")
    if title_tag and title_tag.get_text(strip=True):
        cleaned = _clean_title_string(title_tag.get_text(strip=True))
        if cleaned:
            return cleaned

    return ""


def _clean_title_string(raw_title: str) -> str:
    """Clean company or ATS suffixes from page title strings."""
    if not raw_title:
        return ""

    # "Job Application for Senior Engineer at Acme" -> "Senior Engineer"
    app_for_match = re.search(r"job application for\s+(.*?)\s+at\s+", raw_title, re.I)
    if app_for_match:
        return app_for_match.group(1).strip()

    # Split on common separators: " - ", " | ", " • ", " — "
    parts = re.split(r"\s+[\|\-•—]\s+", raw_title)
    if not parts:
        return raw_title

    # Filter out common portal / generic branding parts
    cleaned_parts: list[str] = []
    ignore_terms = [
        "greenhouse",
        "lever",
        "workday",
        "ashby",
        "careers",
        "jobs",
        "job application",
        "job opening",
        "hiring",
    ]

    for part in parts:
        stripped = part.strip()
        if not stripped:
            continue
        if any(term == stripped.lower() for term in ignore_terms):
            continue
        cleaned_parts.append(stripped)

    if cleaned_parts:
        return cleaned_parts[0]

    return raw_title.strip()


class BatchScannerAgent:
    """Batch intake and URL liveness checker for job postings.

    Features:
    - Verifies active status before triggering downstream LLM extraction.
    - Inspects HTTP status codes (404, 410, 500) and DOM expiration markers.
    - Detects Lever, Greenhouse, Workday, and generic ATS closed notices.
    - Extracts job titles from active postings.
    - Efficient concurrent scanning with deduplication and URL validation.
    """

    def __init__(
        self,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        """Initialize BatchScannerAgent.

        Args:
            default_timeout: Network timeout in seconds for requests.
            max_concurrency: Concurrency limit for batch processing.
            user_agent: Custom User-Agent header for HTTP requests.
        """
        self.default_timeout = default_timeout
        self.max_concurrency = max_concurrency
        self.user_agent = user_agent

    @staticmethod
    def is_valid_url(url: str) -> bool:
        """Validate whether a string is a well-formed HTTP, HTTPS, or file URL."""
        if not isinstance(url, str) or not url.strip():
            return False
        clean = url.strip()
        try:
            parsed = urllib.parse.urlparse(clean)
            if parsed.scheme in ("http", "https"):
                return bool(parsed.netloc)
            if parsed.scheme == "file":
                return bool(parsed.path)
            return False
        except Exception:
            return False

    def _inspect_html(
        self,
        html_content: str,
        url: str = "",
        portal_type: str = "unknown",
    ) -> tuple[bool, str, str]:
        """Inspect HTML content for expiration markers and extract active title.

        Args:
            html_content: Raw HTML markup string.
            url: Target URL for context.
            portal_type: Detected ATS platform name.

        Returns:
            Tuple of (is_active, reason, detected_title).
        """
        if not html_content or not html_content.strip():
            return False, "Empty page content", ""

        soup = BeautifulSoup(html_content, "html.parser")

        # Check for empty DOM
        body_text = soup.get_text(separator=" ", strip=True)
        if len(body_text) < 15:
            return False, "Empty page content", ""

        # 1. High-priority inspection: Alert banners, flash notices, error blocks
        alert_elements = soup.select(
            ".alert, .flash, .error, .closed, .expired, .message, "
            "[role='alert'], #flash_error, .notice, [data-automation-id*='error'], "
            "[data-automation-id*='closed'], .posting-closed"
        )
        for el in alert_elements:
            text = el.get_text(separator=" ", strip=True)
            for pattern, reason_msg in EXPIRATION_PATTERNS:
                if pattern.search(text):
                    logger.info("Found expiration notice in alert banner: %s", reason_msg)
                    return False, reason_msg, ""

        # 2. Heading inspection (h1, h2, h3, title)
        headings_text = " ".join(
            h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3", "title"])
        )
        for pattern, reason_msg in EXPIRATION_PATTERNS:
            if pattern.search(headings_text):
                logger.info("Found expiration notice in heading/title: %s", reason_msg)
                return False, reason_msg, ""

        # 3. Full body text pattern matching
        for pattern, reason_msg in EXPIRATION_PATTERNS:
            if pattern.search(body_text):
                logger.info("Found expiration notice in body text: %s", reason_msg)
                return False, reason_msg, ""

        # 4. Portal-specific heuristic checks
        if portal_type == "lever":
            # Lever closed postings typically have "page-centered" message without an apply button
            has_apply_button = bool(
                soup.select(".postings-btn, .template-btn-submit, a[href*='/apply']")
            )
            if not has_apply_button and any(
                term in body_text.lower()
                for term in [
                    "the job you are looking for is no longer open",
                    "this listing is closed",
                    "no longer accepting applications",
                ]
            ):
                return False, "Lever job listing is closed", ""

        if portal_type == "greenhouse":
            # Greenhouse error flash or 404 text
            if soup.select_one("#flash_error"):
                flash_text = soup.select_one("#flash_error").get_text(strip=True)
                if flash_text:
                    return False, f"Greenhouse error: {flash_text}", ""

        # Page is confirmed active; extract job title
        detected_title = _extract_title_from_soup(soup, portal_type=portal_type)
        return True, "active", detected_title

    def check_html_liveness(
        self,
        html_content: str,
        url: str = "https://example.com/job",
        portal_type: str | None = None,
    ) -> JobLivenessResult:
        """Inspect a raw HTML string for liveness and title extraction.

        Convenient for offline testing, cached DOM analysis, and test suites.

        Args:
            html_content: Raw HTML text of the job posting.
            url: Optional URL reference.
            portal_type: Optional ATS identifier. Inferred from markup if None.

        Returns:
            JobLivenessResult indicating active status, reason, and detected title.
        """
        portal = portal_type or detect_portal_type(url, html_content)
        is_active, reason, detected_title = self._inspect_html(
            html_content, url=url, portal_type=portal
        )
        status_code = 200 if is_active else (404 if "not found" in reason.lower() else 200)

        return JobLivenessResult(
            url=url,
            is_active=is_active,
            status_code=status_code,
            portal_type=portal,
            reason=reason,
            detected_title=detected_title,
        )

    def check_url_liveness(
        self,
        url: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        page: Page | None = None,
    ) -> JobLivenessResult:
        """Check whether a single job URL is active and open.

        Inspects HTTP response status (404, 410, 500, etc.) and analyzes page
        DOM content for ATS expiration markers. Extracts detected job title
        if posting is active.

        Args:
            url: Web URL (http://, https://, or file://) of the posting.
            timeout: Timeout in seconds for page navigation and HTTP requests.
            page: Optional active Playwright Page instance. If provided,
                DOM content is read or navigated using Playwright.

        Returns:
            JobLivenessResult with is_active flag, status code, portal type,
            reason, and detected title.
        """
        if not self.is_valid_url(url):
            return JobLivenessResult(
                url=str(url) if url else "",
                is_active=False,
                status_code=400,
                portal_type=detect_portal_type(str(url) if url else ""),
                reason="Invalid or malformed URL",
                detected_title="",
            )

        clean_url = url.strip()
        portal = detect_portal_type(clean_url)

        # 1. Page instance provided (Playwright)
        if page is not None:
            try:
                # Check if page is already loaded with content (e.g. test set_content)
                current_url = getattr(page, "url", "")
                page_content = page.content() if hasattr(page, "content") else ""

                if current_url == "about:blank" and len(page_content) > 100:
                    # Content already pre-injected into page
                    html_content = page_content
                    status_code = 200
                elif current_url != clean_url:
                    timeout_ms = int(timeout * 1000)
                    response = page.goto(
                        clean_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    status_code = response.status if response else 200
                    html_content = page.content()
                else:
                    status_code = 200
                    html_content = page.content()

                if status_code in (404, 410):
                    return JobLivenessResult(
                        url=clean_url,
                        is_active=False,
                        status_code=status_code,
                        portal_type=portal,
                        reason=f"HTTP {status_code}: Job posting not found",
                        detected_title="",
                    )
                if status_code >= 400:
                    return JobLivenessResult(
                        url=clean_url,
                        is_active=False,
                        status_code=status_code,
                        portal_type=portal,
                        reason=f"HTTP {status_code}: Request failed",
                        detected_title="",
                    )

                is_active, reason, title = self._inspect_html(
                    html_content, url=clean_url, portal_type=portal
                )
                return JobLivenessResult(
                    url=clean_url,
                    is_active=is_active,
                    status_code=status_code,
                    portal_type=portal,
                    reason=reason,
                    detected_title=title,
                )
            except Exception as exc:
                logger.warning("Playwright liveness check error on %s: %s", clean_url, exc)
                return JobLivenessResult(
                    url=clean_url,
                    is_active=False,
                    status_code=0,
                    portal_type=portal,
                    reason=f"Browser navigation error: {exc}",
                    detected_title="",
                )

        # 2. Local file:// URL handling
        if clean_url.startswith("file://"):
            try:
                parsed_path = urllib.request.url2pathname(urllib.parse.urlparse(clean_url).path)
                p = Path(parsed_path)
                if not p.is_file():
                    return JobLivenessResult(
                        url=clean_url,
                        is_active=False,
                        status_code=404,
                        portal_type=portal,
                        reason="Local file not found",
                        detected_title="",
                    )
                html_content = p.read_text(encoding="utf-8", errors="replace")
                is_active, reason, title = self._inspect_html(
                    html_content, url=clean_url, portal_type=portal
                )
                return JobLivenessResult(
                    url=clean_url,
                    is_active=is_active,
                    status_code=200 if is_active else 404,
                    portal_type=portal,
                    reason=reason,
                    detected_title=title,
                )
            except Exception as exc:
                return JobLivenessResult(
                    url=clean_url,
                    is_active=False,
                    status_code=500,
                    portal_type=portal,
                    reason=f"File read error: {exc}",
                    detected_title="",
                )

        # 3. HTTP / HTTPS network request
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        try:
            response = requests.get(
                clean_url,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
            )
            status_code = response.status_code

            if status_code == 404:
                return JobLivenessResult(
                    url=clean_url,
                    is_active=False,
                    status_code=404,
                    portal_type=portal,
                    reason="HTTP 404: Job posting not found",
                    detected_title="",
                )

            if status_code == 410:
                return JobLivenessResult(
                    url=clean_url,
                    is_active=False,
                    status_code=410,
                    portal_type=portal,
                    reason="HTTP 410: Job posting gone / expired",
                    detected_title="",
                )

            if status_code >= 400:
                return JobLivenessResult(
                    url=clean_url,
                    is_active=False,
                    status_code=status_code,
                    portal_type=portal,
                    reason=f"HTTP {status_code}: Error response",
                    detected_title="",
                )

            html_content = response.text
            # Refine portal type from markup if URL was generic
            if portal == "unknown":
                portal = detect_portal_type(clean_url, html_content)

            is_active, reason, title = self._inspect_html(
                html_content, url=clean_url, portal_type=portal
            )

            return JobLivenessResult(
                url=clean_url,
                is_active=is_active,
                status_code=status_code,
                portal_type=portal,
                reason=reason,
                detected_title=title,
            )

        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            return JobLivenessResult(
                url=clean_url,
                is_active=False,
                status_code=status or 0,
                portal_type=portal,
                reason=f"Network request error: {exc}",
                detected_title="",
            )

    def scan_batch(
        self,
        urls: list[str],
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        headless: bool = True,
        drop_invalid: bool = True,
    ) -> BatchScanSummary:
        """Scan a batch of job URLs for liveness and active status.

        Filters out duplicate URLs and invalid URLs. Scans target URLs
        concurrently with thread pooling.

        Args:
            urls: Collection of job URLs to inspect.
            max_concurrency: Maximum parallel scanner threads. Defaults to 5.
            headless: Headless flag for browser automation if browser is used.
            drop_invalid: Whether to filter out invalid/malformed URLs before
                scanning. Defaults to True.

        Returns:
            BatchScanSummary containing total count, active count, inactive count,
            and detailed results list.
        """
        if not urls:
            return BatchScanSummary(
                total=0,
                active_count=0,
                inactive_count=0,
                results=[],
            )

        # 1. Deduplicate while preserving order
        unique_urls: list[str] = []
        seen: set[str] = set()

        for raw in urls:
            if not isinstance(raw, str):
                continue
            cleaned = raw.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            unique_urls.append(cleaned)

        # 2. Filter out invalid URLs
        valid_urls: list[str] = []
        invalid_results: list[JobLivenessResult] = []

        for candidate in unique_urls:
            if self.is_valid_url(candidate):
                valid_urls.append(candidate)
            elif not drop_invalid:
                invalid_results.append(
                    JobLivenessResult(
                        url=candidate,
                        is_active=False,
                        status_code=400,
                        portal_type=detect_portal_type(candidate),
                        reason="Invalid URL format",
                        detected_title="",
                    )
                )

        workers = max(1, min(max_concurrency or self.max_concurrency, len(valid_urls) or 1))
        scanned_results: list[JobLivenessResult] = []

        # 3. Concurrent scan execution
        if valid_urls:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_url = {
                    executor.submit(self.check_url_liveness, u, self.default_timeout): u
                    for u in valid_urls
                }
                # Preserve URL ordering in results
                results_by_url: dict[str, JobLivenessResult] = {}
                for future in concurrent.futures.as_completed(future_to_url):
                    u = future_to_url[future]
                    try:
                        res = future.result()
                        results_by_url[u] = res
                    except Exception as exc:
                        results_by_url[u] = JobLivenessResult(
                            url=u,
                            is_active=False,
                            status_code=0,
                            portal_type=detect_portal_type(u),
                            reason=f"Scan error: {exc}",
                            detected_title="",
                        )

                scanned_results = [results_by_url[u] for u in valid_urls if u in results_by_url]

        all_results = scanned_results + invalid_results
        active_cnt = sum(1 for r in all_results if r.is_active)
        inactive_cnt = sum(1 for r in all_results if not r.is_active)

        return BatchScanSummary(
            total=len(all_results),
            active_count=active_cnt,
            inactive_count=inactive_cnt,
            results=all_results,
        )

    def filter_active_urls(
        self,
        urls: list[str],
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        headless: bool = True,
    ) -> list[str]:
        """Convenience helper returning only verified active job URLs.

        Filters out expired, closed, duplicate, or invalid URLs to prevent
        wasting LLM quota and downstream processing time.

        Args:
            urls: Collection of job URLs to inspect.
            max_concurrency: Maximum parallel scanner threads. Defaults to 5.
            headless: Headless flag for browser automation if browser is used.

        Returns:
            List of confirmed active job URLs.
        """
        summary = self.scan_batch(
            urls,
            max_concurrency=max_concurrency,
            headless=headless,
            drop_invalid=True,
        )
        return [result.url for result in summary.results if result.is_active]


# Module-level convenience functions
def check_url_liveness(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    page: Page | None = None,
) -> JobLivenessResult:
    """Check whether a single job URL is active and open."""
    agent = BatchScannerAgent(default_timeout=timeout)
    return agent.check_url_liveness(url=url, timeout=timeout, page=page)


def scan_batch(
    urls: list[str],
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    headless: bool = True,
) -> BatchScanSummary:
    """Scan a batch of job URLs for liveness and active status."""
    agent = BatchScannerAgent(max_concurrency=max_concurrency)
    return agent.scan_batch(urls=urls, max_concurrency=max_concurrency, headless=headless)


def filter_active_urls(
    urls: list[str],
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    headless: bool = True,
) -> list[str]:
    """Filter a list of job URLs, returning only verified active URLs."""
    agent = BatchScannerAgent(max_concurrency=max_concurrency)
    return agent.filter_active_urls(urls=urls, max_concurrency=max_concurrency, headless=headless)


__all__ = [
    "JobLivenessResult",
    "BatchScanSummary",
    "BatchScannerAgent",
    "check_url_liveness",
    "scan_batch",
    "filter_active_urls",
    "detect_portal_type",
]
