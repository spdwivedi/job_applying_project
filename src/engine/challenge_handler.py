"""CAPTCHA & Security Challenge Interceptor for JobApplier-Core.

Detects interactive security challenges (Cloudflare Turnstile, Google reCAPTCHA,
hCaptcha, Workday, and generic bot-verification gates) across the main Playwright
page DOM and all child frames. When detected, suspends automation WITHOUT closing
the browser/context/page, emits ``EVENT_SECURITY_CHALLENGE_DETECTED``, notifies the
FastAPI HITL dashboard over WebSockets, and waits for human resolution.

This module strictly observes and pauses for manual human resolution; it never
attempts to auto-solve or bypass any CAPTCHA or anti-bot gate.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv
from pydantic import BaseModel, Field

try:
    from playwright.sync_api import Page
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    Page = Any  # type: ignore

try:
    from websockets.sync.client import connect as ws_sync_connect
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    ws_sync_connect = None  # type: ignore

logger = logging.getLogger(__name__)

# Canonical event & UI constants
EVENT_SECURITY_CHALLENGE_DETECTED: Final[str] = "EVENT_SECURITY_CHALLENGE_DETECTED"
EVENT_SECURITY_CHALLENGE_RESOLVED: Final[str] = "EVENT_SECURITY_CHALLENGE_RESOLVED"
DEFAULT_CHALLENGE_MESSAGE: Final[str] = "CAPTCHA Detected: Complete in Browser"
DEFAULT_UI_PORT: Final[int] = 8501

# Challenge category identifiers
CHALLENGE_CLOUDFLARE_TURNSTILE: Final[str] = "cloudflare_turnstile"
CHALLENGE_GOOGLE_RECAPTCHA: Final[str] = "google_recaptcha"
CHALLENGE_HCAPTCHA: Final[str] = "hcaptcha"
CHALLENGE_WORKDAY_BOT: Final[str] = "workday_bot_challenge"
CHALLENGE_GENERIC_BOT: Final[str] = "generic_bot_challenge"

# Phrase patterns indicative of Workday or generic bot-verification gates
_BOT_CHALLENGE_TEXT_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("verify_human_phrase", re.compile(r"\bverify\s+(?:that\s+)?you(?:'re|\s+are)\s+human\b", re.IGNORECASE)),
    ("prove_human_phrase", re.compile(r"\b(?:prove|confirm)\s+(?:that\s+)?you(?:'re|\s+are)\s+human\b", re.IGNORECASE)),
    ("not_a_robot_phrase", re.compile(r"\b(?:i(?:'m|\s+am)\s+not\s+a\s+robot|are\s+you\s+a\s+robot)\b", re.IGNORECASE)),
    ("bot_verification_phrase", re.compile(r"\bbot\s+verification\b", re.IGNORECASE)),
    ("human_verification_phrase", re.compile(r"\bhuman\s+verification\b", re.IGNORECASE)),
    ("security_challenge_phrase", re.compile(r"\bsecurity\s+challenge\b", re.IGNORECASE)),
    ("security_verification_phrase", re.compile(r"\bsecurity\s+verification\b", re.IGNORECASE)),
    ("complete_security_check", re.compile(r"\bcomplete\s+the\s+security\s+(?:check|challenge)\b", re.IGNORECASE)),
    ("solve_captcha_phrase", re.compile(r"\b(?:complete|solve)\s+the\s+captcha\b", re.IGNORECASE)),
    ("captcha_verification_phrase", re.compile(r"\bcaptcha\s+(?:verification|challenge)\b", re.IGNORECASE)),
    ("checking_browser_phrase", re.compile(r"\bchecking\s+your\s+browser\s+before\s+accessing\b", re.IGNORECASE)),
    (
        "unusual_traffic_phrase",
        re.compile(r"\bunusual\s+traffic\b.*\b(?:automated|robot|captcha|verify)\b", re.IGNORECASE | re.DOTALL),
    ),
)

_CHALLENGE_ATTR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[-_])(captcha|px-captcha|bot-check|bot_check|security-challenge|security_challenge|arkose|funcaptcha|datadome)(?:$|[-_])",
    re.IGNORECASE,
)

_WORKDAY_AUTOMATION_CHALLENGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(captcha|challenge|securitycheck|botverify|botcheck|humanverify)",
    re.IGNORECASE,
)


class ChallengeEvent(BaseModel):
    """Structured security or CAPTCHA challenge event emitted to listeners and dashboard."""

    event: str = Field(
        default=EVENT_SECURITY_CHALLENGE_DETECTED,
        description="Event identifier string.",
    )
    challenge_type: str = Field(
        ...,
        description="Detected challenge provider or category.",
    )
    url: str = Field(
        default="",
        description="URL of the main page where the challenge was detected.",
    )
    frame_url: str = Field(
        default="",
        description="URL of the child frame containing the challenge, if applicable.",
    )
    message: str = Field(
        default=DEFAULT_CHALLENGE_MESSAGE,
        description="Human-facing dashboard banner notification message.",
    )
    markers: list[str] = Field(
        default_factory=list,
        description="DOM or URL markers matched during inspection.",
    )
    detected_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="UTC ISO-8601 timestamp when the challenge was recorded.",
    )
    resolved: bool = Field(
        default=False,
        description="Whether the security challenge has been resolved.",
    )


ChallengeCallback = Callable[[ChallengeEvent], Any]


def resolve_ui_port(env_file: str | Path | None = None) -> int:
    """Resolve HITL dashboard port from environment or ``.env``, defaulting to ``8501``."""
    load_dotenv(dotenv_path=env_file, override=False)
    raw_port = os.getenv("UI_PORT") or ""
    if not raw_port.strip():
        return DEFAULT_UI_PORT
    try:
        parsed = int(raw_port.strip())
        if 1 <= parsed <= 65535:
            return parsed
    except ValueError:
        pass
    return DEFAULT_UI_PORT


def resolve_challenge_ws_url(
    ws_url: str | None = None,
    *,
    ui_port: int | None = None,
    env_file: str | Path | None = None,
) -> str:
    """Resolve the WebSocket URL for the FastAPI challenge notification endpoint."""
    if ws_url and ws_url.strip():
        return ws_url.strip()
    port = ui_port if ui_port is not None else resolve_ui_port(env_file=env_file)
    return f"ws://127.0.0.1:{port}/ws/challenges"


def _attr_values_as_text(tag: Tag, attr: str) -> str:
    """Return an HTML tag attribute value normalized as a lowercase string."""
    val = tag.get(attr)
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        return " ".join(str(item) for item in val).strip().lower()
    return str(val).strip().lower()


def _classes_as_list(tag: Tag) -> list[str]:
    """Return CSS classes on an HTML tag normalized to lowercase strings."""
    cls = tag.get("class")
    if not cls:
        return []
    if isinstance(cls, (list, tuple)):
        return [str(c).strip().lower() for c in cls if str(c).strip()]
    return [c.strip().lower() for c in str(cls).split() if c.strip()]


def inspect_frame_url(frame_url: str) -> tuple[str, list[str]] | None:
    """Inspect a frame URL for known CAPTCHA/challenge service origins."""
    if not frame_url:
        return None
    lower_url = frame_url.strip().lower()
    if not lower_url or lower_url in ("about:blank", "about:srcdoc"):
        return None

    parsed = urlparse(lower_url)
    host = parsed.netloc
    path_and_query = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path

    # 1. hCaptcha
    if "hcaptcha.com" in host or "assets.hcaptcha.com" in lower_url or "hcaptcha.com/" in lower_url:
        return CHALLENGE_HCAPTCHA, [f"frame_url:{frame_url}"]

    # 2. Cloudflare Turnstile
    if (
        "challenges.cloudflare.com" in host
        or "challenges.cloudflare.com" in lower_url
        or "/cdn-cgi/challenge-platform" in path_and_query
        or "turnstile" in lower_url
    ):
        return CHALLENGE_CLOUDFLARE_TURNSTILE, [f"frame_url:{frame_url}"]

    # 3. Google reCAPTCHA
    if (
        "google.com/recaptcha" in lower_url
        or "recaptcha.net/recaptcha" in lower_url
        or "gstatic.com/recaptcha" in lower_url
        or "recaptcha/api" in lower_url
    ):
        return CHALLENGE_GOOGLE_RECAPTCHA, [f"frame_url:{frame_url}"]

    # 4. Generic / third-party challenge frames
    if "captcha-delivery.com" in host or "arkoselabs.com" in host or "funcaptcha.com" in host:
        challenge_type = (
            CHALLENGE_WORKDAY_BOT if "workday" in lower_url else CHALLENGE_GENERIC_BOT
        )
        return challenge_type, [f"frame_url:{frame_url}"]

    return None


def _is_workday_context(soup: BeautifulSoup, url: str, frame_url: str, raw_html: str) -> bool:
    """Determine whether the inspected document/URL represents a Workday portal context."""
    combined_url = f"{url} {frame_url}".lower()
    if "workday" in combined_url or "myworkdayjobs" in combined_url:
        return True
    if soup.find(attrs={"data-automation-id": True}) is not None:
        return True
    if "workday" in raw_html.lower() and (
        soup.find(attrs={"id": re.compile(r"^wd-", re.IGNORECASE)}) is not None
        or soup.find(attrs={"class": re.compile(r"\bwd-", re.IGNORECASE)}) is not None
    ):
        return True
    return False


def inspect_html_for_challenge(
    html: str,
    *,
    url: str = "",
    frame_url: str = "",
) -> dict[str, Any] | None:
    """Inspect an HTML document or frame snippet for CAPTCHA or bot-challenge markers.

    Args:
        html: Raw HTML markup of the page or frame.
        url: Main page URL for context.
        frame_url: Frame URL if inspecting an embedded frame.

    Returns:
        Dictionary matching ``ChallengeEvent`` fields if a challenge is detected, else ``None``.
    """
    frame_match = inspect_frame_url(frame_url)
    if frame_match is not None:
        ctype, markers = frame_match
        return ChallengeEvent(
            event=EVENT_SECURITY_CHALLENGE_DETECTED,
            challenge_type=ctype,
            url=url,
            frame_url=frame_url,
            message=DEFAULT_CHALLENGE_MESSAGE,
            markers=markers,
            resolved=False,
        ).model_dump()

    if not html or not isinstance(html, str):
        return None

    soup = BeautifulSoup(html, "html.parser")

    # 1. hCaptcha markers (checked before generic g-recaptcha-response because hCaptcha emits a compat textarea)
    hcaptcha_markers: list[str] = []
    for tag in soup.find_all(True):
        classes = _classes_as_list(tag)
        tag_id = _attr_values_as_text(tag, "id")
        if any("h-captcha" in c or "hcaptcha" in c for c in classes) or "h-captcha" in tag_id or "hcaptcha" in tag_id:
            hcaptcha_markers.append(f"dom:{tag.name}#{tag_id or 'h-captcha'}")
        if tag.has_attr("data-hcaptcha-sitekey") or tag.has_attr("data-hcaptcha-widget-id"):
            hcaptcha_markers.append(f"attr:hcaptcha:{tag.name}")
        if tag.name in ("input", "textarea") and _attr_values_as_text(tag, "name") == "h-captcha-response":
            hcaptcha_markers.append("input:h-captcha-response")

    for iframe in soup.find_all("iframe"):
        src = _attr_values_as_text(iframe, "src")
        title = _attr_values_as_text(iframe, "title")
        if "hcaptcha.com" in src or "assets.hcaptcha.com" in src:
            hcaptcha_markers.append(f"iframe_src:{src}")
        if "hcaptcha" in title:
            hcaptcha_markers.append(f"iframe_title:{title}")

    for script in soup.find_all("script"):
        src = _attr_values_as_text(script, "src")
        if "hcaptcha.com" in src or "assets.hcaptcha.com" in src:
            hcaptcha_markers.append(f"script_src:{src}")

    if hcaptcha_markers:
        return ChallengeEvent(
            event=EVENT_SECURITY_CHALLENGE_DETECTED,
            challenge_type=CHALLENGE_HCAPTCHA,
            url=url,
            frame_url=frame_url,
            message=DEFAULT_CHALLENGE_MESSAGE,
            markers=list(dict.fromkeys(hcaptcha_markers)),
            resolved=False,
        ).model_dump()

    # 2. Cloudflare Turnstile markers
    turnstile_markers: list[str] = []
    for tag in soup.find_all(True):
        classes = _classes_as_list(tag)
        tag_id = _attr_values_as_text(tag, "id")
        if (
            any("cf-turnstile" in c or c.startswith("turnstile") for c in classes)
            or "cf-turnstile" in tag_id
            or tag_id in ("turnstile-wrapper", "challenge-form", "challenge-running", "cf-challenge-running")
            or ("turnstile" in tag_id and tag.name in ("div", "section", "form", "span"))
        ):
            turnstile_markers.append(f"dom:{tag.name}#{tag_id or 'cf-turnstile'}")
        if tag.name in ("input", "textarea") and _attr_values_as_text(tag, "name") == "cf-turnstile-response":
            turnstile_markers.append("input:cf-turnstile-response")

    for iframe in soup.find_all("iframe"):
        src = _attr_values_as_text(iframe, "src")
        title = _attr_values_as_text(iframe, "title")
        if "challenges.cloudflare.com" in src or "turnstile" in src:
            turnstile_markers.append(f"iframe_src:{src}")
        if "cloudflare" in title or "turnstile" in title:
            turnstile_markers.append(f"iframe_title:{title}")

    for script in soup.find_all("script"):
        src = _attr_values_as_text(script, "src")
        if "challenges.cloudflare.com" in src or "turnstile" in src:
            turnstile_markers.append(f"script_src:{src}")

    if turnstile_markers:
        return ChallengeEvent(
            event=EVENT_SECURITY_CHALLENGE_DETECTED,
            challenge_type=CHALLENGE_CLOUDFLARE_TURNSTILE,
            url=url,
            frame_url=frame_url,
            message=DEFAULT_CHALLENGE_MESSAGE,
            markers=list(dict.fromkeys(turnstile_markers)),
            resolved=False,
        ).model_dump()

    # 3. Google reCAPTCHA markers
    recaptcha_markers: list[str] = []
    for tag in soup.find_all(True):
        classes = _classes_as_list(tag)
        tag_id = _attr_values_as_text(tag, "id")
        if (
            any("g-recaptcha" in c or c == "recaptcha" for c in classes)
            or "g-recaptcha" in tag_id
            or tag_id == "recaptcha"
        ):
            recaptcha_markers.append(f"dom:{tag.name}#{tag_id or 'g-recaptcha'}")
        if tag.name in ("input", "textarea") and _attr_values_as_text(tag, "name") == "g-recaptcha-response":
            recaptcha_markers.append("input:g-recaptcha-response")
        title = _attr_values_as_text(tag, "title")
        if "recaptcha" in title:
            recaptcha_markers.append(f"title:{tag.name}:{title}")

    for iframe in soup.find_all("iframe"):
        src = _attr_values_as_text(iframe, "src")
        title = _attr_values_as_text(iframe, "title")
        if "google.com/recaptcha" in src or "recaptcha.net/recaptcha" in src or "recaptcha/api" in src:
            recaptcha_markers.append(f"iframe_src:{src}")
        if "recaptcha" in title:
            recaptcha_markers.append(f"iframe_title:{title}")

    for script in soup.find_all("script"):
        src = _attr_values_as_text(script, "src")
        if "google.com/recaptcha" in src or "recaptcha.net/recaptcha" in src or "recaptcha/api" in src:
            recaptcha_markers.append(f"script_src:{src}")

    if recaptcha_markers:
        return ChallengeEvent(
            event=EVENT_SECURITY_CHALLENGE_DETECTED,
            challenge_type=CHALLENGE_GOOGLE_RECAPTCHA,
            url=url,
            frame_url=frame_url,
            message=DEFAULT_CHALLENGE_MESSAGE,
            markers=list(dict.fromkeys(recaptcha_markers)),
            resolved=False,
        ).model_dump()

    # 4. Workday & Generic Bot-Challenge markers
    bot_markers: list[str] = []

    for tag in soup.find_all(True):
        auto_id = _attr_values_as_text(tag, "data-automation-id")
        if auto_id and _WORKDAY_AUTOMATION_CHALLENGE_PATTERN.search(auto_id):
            bot_markers.append(f"data-automation-id:{auto_id}")

        tag_id = _attr_values_as_text(tag, "id")
        classes_text = " ".join(_classes_as_list(tag))
        if tag_id and _CHALLENGE_ATTR_PATTERN.search(tag_id):
            bot_markers.append(f"id:{tag_id}")
        if classes_text and _CHALLENGE_ATTR_PATTERN.search(classes_text):
            bot_markers.append(f"class:{classes_text}")

    for iframe in soup.find_all("iframe"):
        src = _attr_values_as_text(iframe, "src")
        title = _attr_values_as_text(iframe, "title")
        if "captcha" in src or "security-challenge" in src or "bot" in src:
            bot_markers.append(f"iframe_src:{src}")
        if any(kw in title for kw in ("captcha", "security challenge", "bot verification", "human verification")):
            bot_markers.append(f"iframe_title:{title}")

    # Inspect visible text (excluding script/style blocks) for human/bot verification prompts
    text_soup = BeautifulSoup(html, "html.parser")
    for hidden_tag in text_soup(["script", "style", "noscript"]):
        hidden_tag.decompose()
    visible_text = re.sub(r"\s+", " ", text_soup.get_text(" ", strip=True))
    title_text = ""
    if soup.title and soup.title.string:
        title_text = soup.title.string.strip()
    combined_text = f"{title_text} {visible_text}".strip()

    for marker_name, pattern in _BOT_CHALLENGE_TEXT_PATTERNS:
        if pattern.search(combined_text):
            bot_markers.append(f"text:{marker_name}")

    if bot_markers:
        is_workday = _is_workday_context(soup, url, frame_url, html)
        challenge_type = CHALLENGE_WORKDAY_BOT if is_workday else CHALLENGE_GENERIC_BOT
        return ChallengeEvent(
            event=EVENT_SECURITY_CHALLENGE_DETECTED,
            challenge_type=challenge_type,
            url=url,
            frame_url=frame_url,
            message=DEFAULT_CHALLENGE_MESSAGE,
            markers=list(dict.fromkeys(bot_markers)),
            resolved=False,
        ).model_dump()

    return None


class ChallengeDetector:
    """Playwright CAPTCHA & bot-challenge detector with HITL pause & WebSocket alert support."""

    def __init__(
        self,
        *,
        ws_url: str | None = None,
        ui_port: int | None = None,
        env_file: str | Path | None = None,
        listeners: list[ChallengeCallback] | None = None,
        on_challenge: ChallengeCallback | None = None,
        ws_connect: Callable[..., Any] | None = None,
        notify_dashboard_enabled: bool = True,
        ws_timeout: float = 1.5,
    ) -> None:
        self.ws_url_override = ws_url
        self.ui_port_override = ui_port
        self.env_file = env_file
        self.listeners: list[ChallengeCallback] = list(listeners or [])
        if on_challenge is not None and on_challenge not in self.listeners:
            self.listeners.append(on_challenge)
        self._ws_connect = ws_connect
        self.notify_dashboard_enabled = notify_dashboard_enabled
        self.ws_timeout = ws_timeout

        self.suspended: bool = False
        self.active_challenge: ChallengeEvent | None = None
        self.events: list[ChallengeEvent] = []
        self._manual_resolved_event = threading.Event()
        self._in_wait_loop: bool = False

    def get_ws_url(self) -> str:
        """Return the resolved WebSocket notification URL for the HITL dashboard."""
        return resolve_challenge_ws_url(
            self.ws_url_override,
            ui_port=self.ui_port_override,
            env_file=self.env_file,
        )

    def add_listener(self, callback: ChallengeCallback) -> None:
        """Register an event callback invoked whenever a challenge event is emitted."""
        if callback not in self.listeners:
            self.listeners.append(callback)

    def remove_listener(self, callback: ChallengeCallback) -> None:
        """Remove a previously registered event callback."""
        self.listeners = [cb for cb in self.listeners if cb is not callback]

    def signal_resolved(self) -> None:
        """Signal manual resolution from external code or UI callbacks."""
        self._manual_resolved_event.set()
        self.suspended = False

    def reset_resolution_signal(self) -> None:
        """Reset any pending manual resolution signal."""
        self._manual_resolved_event.clear()

    def notify_dashboard(self, event: ChallengeEvent) -> bool:
        """Send a challenge event payload to the FastAPI dashboard WebSocket endpoint.

        Handles connection failures gracefully when the dashboard server is offline
        so browser automation and offline tests never crash.
        """
        if not self.notify_dashboard_enabled:
            return False

        connector = self._ws_connect if self._ws_connect is not None else ws_sync_connect
        if connector is None:
            logger.debug("WebSocket client unavailable; skipping dashboard challenge notification.")
            return False

        target_url = self.get_ws_url()
        payload = event.model_dump_json()

        try:
            connect_kwargs: dict[str, Any] = {}
            if self._ws_connect is None:
                connect_kwargs["open_timeout"] = self.ws_timeout
                connect_kwargs["close_timeout"] = self.ws_timeout
            with connector(target_url, **connect_kwargs) as websocket:
                websocket.send(payload)
            return True
        except Exception as exc:
            logger.debug(
                "Could not deliver WebSocket challenge notification to %s (dashboard offline?): %s",
                target_url,
                exc,
            )
            return False

    def _emit_event(self, event: ChallengeEvent) -> None:
        """Record, log, broadcast to listeners, and push to the dashboard WebSocket."""
        self.events.append(event)
        log_fn = logger.info if event.resolved else logger.warning
        log_fn(
            "%s [%s] url=%s message=%s",
            event.event,
            event.challenge_type,
            event.url or "<unknown>",
            event.message,
            extra={
                "event": event.event,
                "challenge_type": event.challenge_type,
                "url": event.url,
                "frame_url": event.frame_url,
                "resolved": event.resolved,
                "markers": event.markers,
            },
        )

        for callback in list(self.listeners):
            try:
                callback(event)
            except Exception as exc:
                logger.debug("Challenge event listener failed cleanly: %s", exc)

        self.notify_dashboard(event)

    def detect_challenge_from_html(
        self,
        html: str,
        *,
        url: str = "",
        frame_url: str = "",
        emit: bool = True,
    ) -> dict[str, Any] | None:
        """Inspect raw HTML for security challenges and optionally emit/notify."""
        detected = inspect_html_for_challenge(html, url=url, frame_url=frame_url)
        if detected is None:
            self.suspended = False
            if not self._in_wait_loop:
                self.active_challenge = None
            return None

        event = ChallengeEvent(**detected)
        self.suspended = True
        if emit and not self._in_wait_loop:
            self.active_challenge = event
            self._emit_event(event)
        return event.model_dump()

    def _scan_page_without_side_effects(self, page: Page) -> dict[str, Any] | None:
        """Inspect active Playwright page DOM and all ``page.frames`` without mutating state."""
        if page is None:
            return None
        if hasattr(page, "is_closed") and page.is_closed():
            return None

        page_url = ""
        try:
            page_url = str(getattr(page, "url", "") or "")
        except Exception:
            page_url = ""

        # 1. Inspect main page DOM content
        try:
            main_html = page.content()
        except Exception as exc:
            logger.debug("Unable to read main page content during challenge scan: %s", exc)
            main_html = ""

        if isinstance(main_html, str) and main_html:
            found = inspect_html_for_challenge(main_html, url=page_url)
            if found is not None:
                return found

        # 2. Inspect all frames (URLs and frame DOM content)
        try:
            frames = list(getattr(page, "frames", []) or [])
        except Exception:
            frames = []

        for frame in frames:
            frame_url = ""
            try:
                frame_url = str(getattr(frame, "url", "") or "")
            except Exception:
                frame_url = ""

            frame_url_hit = inspect_frame_url(frame_url)
            if frame_url_hit is not None:
                ctype, markers = frame_url_hit
                return ChallengeEvent(
                    event=EVENT_SECURITY_CHALLENGE_DETECTED,
                    challenge_type=ctype,
                    url=page_url,
                    frame_url=frame_url,
                    message=DEFAULT_CHALLENGE_MESSAGE,
                    markers=markers,
                    resolved=False,
                ).model_dump()

            try:
                frame_html = frame.content()
            except Exception:
                frame_html = ""

            if isinstance(frame_html, str) and frame_html:
                found_in_frame = inspect_html_for_challenge(
                    frame_html,
                    url=page_url,
                    frame_url=frame_url,
                )
                if found_in_frame is not None:
                    return found_in_frame

        return None

    def detect_challenge(self, page: Page) -> dict[str, Any] | None:
        """Inspect main page DOM and all ``page.frames`` for CAPTCHA/bot challenges.

        When detected, suspends automation without closing browser/context/page,
        emits ``EVENT_SECURITY_CHALLENGE_DETECTED``, and sends a WebSocket alert
        to the HITL dashboard.
        """
        detected = self._scan_page_without_side_effects(page)
        if detected is None:
            self.suspended = False
            if not self._in_wait_loop:
                self.active_challenge = None
            return None

        event = ChallengeEvent(**detected)
        self.suspended = True
        if not self._in_wait_loop:
            self.active_challenge = event
            self._emit_event(event)
        return event.model_dump()

    def _complete_resolution(self, prior_event: ChallengeEvent | None) -> None:
        """Clear suspension state and emit a resolution event to listeners and dashboard."""
        self.suspended = False
        self.active_challenge = None
        self._manual_resolved_event.clear()

        if prior_event is not None:
            resolved_event = ChallengeEvent(
                event=EVENT_SECURITY_CHALLENGE_RESOLVED,
                challenge_type=prior_event.challenge_type,
                url=prior_event.url,
                frame_url=prior_event.frame_url,
                message="Security challenge resolved; resuming automation.",
                markers=prior_event.markers,
                resolved=True,
            )
            self._emit_event(resolved_event)

    def wait_for_resolution(
        self,
        page: Page,
        timeout_seconds: float = 120.0,
        poll_interval: float = 1.0,
        *,
        resolution_signal: threading.Event | Callable[[], bool] | None = None,
    ) -> bool:
        """Poll without closing Playwright context until challenge clears or manual signal arrives.

        Args:
            page: Active Playwright page (never closed by this method).
            timeout_seconds: Maximum wait duration in seconds before timing out.
            poll_interval: Polling interval in seconds between DOM/frame checks.
            resolution_signal: Optional ``threading.Event`` or zero-arg predicate signaling
                manual human resolution.

        Returns:
            ``True`` when the challenge disappears or manual resolution is signaled;
            ``False`` if timeout expires or the page is closed prematurely.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        step = max(0.02, float(poll_interval))

        prior_event = self.active_challenge
        # If caller invokes wait_for_resolution directly without prior detect_challenge,
        # let the very first detection emit once before silencing subsequent polls.
        first_poll = prior_event is None

        try:
            while True:
                if self._manual_resolved_event.is_set():
                    self._complete_resolution(prior_event)
                    return True

                if resolution_signal is not None:
                    try:
                        signaled = (
                            resolution_signal.is_set()
                            if isinstance(resolution_signal, threading.Event)
                            else bool(resolution_signal())
                        )
                        if signaled:
                            self._complete_resolution(prior_event)
                            return True
                    except Exception:
                        pass

                if page is not None and hasattr(page, "is_closed") and page.is_closed():
                    logger.warning("Playwright page closed while waiting for security challenge resolution.")
                    self.suspended = False
                    self.active_challenge = None
                    return False

                self._in_wait_loop = not first_poll
                current = self.detect_challenge(page)
                first_poll = False
                self._in_wait_loop = True

                if current is None:
                    self._complete_resolution(prior_event or self.active_challenge)
                    return True

                if prior_event is None:
                    prior_event = ChallengeEvent(**current)
                    self.active_challenge = prior_event

                self.suspended = True

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Timed out waiting %.1fs for security challenge resolution (%s).",
                        timeout_seconds,
                        prior_event.challenge_type,
                    )
                    return False

                wait_chunk = min(step, remaining)
                if self._manual_resolved_event.wait(timeout=wait_chunk):
                    self._complete_resolution(prior_event)
                    return True
        finally:
            self._in_wait_loop = False

    def guard_page(
        self,
        page: Page,
        timeout_seconds: float = 120.0,
        poll_interval: float = 1.0,
        *,
        resolution_signal: threading.Event | Callable[[], bool] | None = None,
    ) -> bool:
        """Detect any challenge on ``page`` and pause until resolved if found.

        Returns:
            ``True`` if no challenge is present or the challenge is resolved within timeout;
            ``False`` if a challenge remains unresolved when timeout expires.
        """
        detected = self.detect_challenge(page)
        if detected is None:
            return True
        return self.wait_for_resolution(
            page,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            resolution_signal=resolution_signal,
        )


__all__ = [
    "CHALLENGE_CLOUDFLARE_TURNSTILE",
    "CHALLENGE_GENERIC_BOT",
    "CHALLENGE_GOOGLE_RECAPTCHA",
    "CHALLENGE_HCAPTCHA",
    "CHALLENGE_WORKDAY_BOT",
    "DEFAULT_CHALLENGE_MESSAGE",
    "DEFAULT_UI_PORT",
    "EVENT_SECURITY_CHALLENGE_DETECTED",
    "EVENT_SECURITY_CHALLENGE_RESOLVED",
    "ChallengeCallback",
    "ChallengeDetector",
    "ChallengeEvent",
    "inspect_frame_url",
    "inspect_html_for_challenge",
    "resolve_challenge_ws_url",
    "resolve_ui_port",
]
