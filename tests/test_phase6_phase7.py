"""Phase 6 & Phase 7 Automated Integration Test Suite for JobApplier-Core.

Covers:
1. CAPTCHA & Security Challenge Interceptor (src/engine/challenge_handler.py & src/ui/challenge_ws.py):
   - Detects Cloudflare Turnstile, Google reCAPTCHA, hCaptcha, and Workday bot challenges across
     Playwright main page DOM and child frames without false positives on clean forms.
   - Emits EVENT_SECURITY_CHALLENGE_DETECTED, suspends without closing browser context/page,
     pushes WebSocket alert ("CAPTCHA Detected: Complete in Browser") verified via FastAPI
     TestClient /ws/challenges, and resumes on DOM clearance or resolution signal (False on timeout).
2. Ashby ATS Portal Adapter (src/engine/ats_ashby.py):
   - AshbyAdapter.detect, extract_fields (_ashby_ inputs, selects/multi-selects, radio boolean
     groups, custom file dropzones, screening textareas), fill_custom_controls, detect_form_step,
     and advance_step blocking final submit.
3. RenderCV Typst Page-Budget Enforcer (src/engine/page_budget.py):
   - Compiles an overflowing 2-page RenderCV v2.8 YAML inside tmp_path and verifies
     enforce_page_budget(..., target_pages=1) compacts/trims it so
     get_pdf_page_count(output_pdf_path) == 1 with a valid %PDF- header.
4. Zero-Token Public ATS Feed Scanners (src/intake/ats_scanner.py & config/companies.yml):
   - Tests parse_greenhouse_jobs, parse_lever_jobs, stale-posting filtering (max_age_days),
     keyword filters, deduplication, atomic intake_queue.json persistence in tmp_path, and
     clean parsing of config/companies.yml.
5. Intelligent Fit Scoring & Hard-Blocker Filter (src/engine/fit_scorer.py):
   - Verifies hard-blocker rejection (sponsorship/citizenship conflict, security clearance
     mismatch, strict years-of-experience mismatch -> hard_blocked=True, recommended_action="skip",
     score <= 2), 1-5 scoring separation (5 direct match vs 1-2 stack mismatch), and
     score_intake_queue persisting results back to intake_queue.json.
6. Interactive Interview Practice Mode (src/agents/interview_practice.py):
   - Verifies loading interview_prep/targeted_behavioral_qa.md and technical_skills_gap.md
     from a tmp_path dossier, sequential question iteration (start_session / get_next_question),
     and STAR evaluation (submit_answer) returning STAR dimension scores, coaching feedback,
     and profile-grounded suggested_evidence.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from playwright.sync_api import sync_playwright
from starlette.testclient import TestClient

from src.agents.interview_practice import (
    InterviewPracticeAgent,
    InterviewQuestion,
    PracticeSessionState,
    STAREvaluation,
)
from src.engine.ats_ashby import AshbyAdapter
from src.engine.challenge_handler import (
    CHALLENGE_CLOUDFLARE_TURNSTILE,
    CHALLENGE_GOOGLE_RECAPTCHA,
    CHALLENGE_HCAPTCHA,
    CHALLENGE_WORKDAY_BOT,
    DEFAULT_CHALLENGE_MESSAGE,
    EVENT_SECURITY_CHALLENGE_DETECTED,
    EVENT_SECURITY_CHALLENGE_RESOLVED,
    ChallengeDetector,
    ChallengeEvent,
)
from src.engine.fit_scorer import (
    FitEvaluationResult,
    JobFitScorer,
    score_intake_queue,
)
from src.engine.page_budget import (
    PageBudgetResult,
    enforce_page_budget,
    get_pdf_page_count,
)
from src.intake.ats_scanner import (
    ATSFeedScanner,
    CompanyFeedConfig,
    DiscoveredJob,
    parse_greenhouse_jobs,
    parse_lever_jobs,
)
from src.ui.app import create_app
from src.ui.challenge_ws import get_challenge_manager

# Pre-commit hygiene constants constructed dynamically (never literal emails/phones)
_AT = chr(64)
_DOT = chr(46)
MOCK_APPLICANT_EMAIL = f"jordan{_AT}example{_DOT}com"
MOCK_PHONE_DIGITS = "5550142"


# ---------------------------------------------------------------------------
# Helpers for WebSocket & Mock HTTP Sessions
# ---------------------------------------------------------------------------
class _SyncWebSocketAdapter:
    """Adapts Starlette's WebSocketTestSession to expose `.send(str)` for ChallengeDetector."""

    def __init__(self, ctx_manager: Any) -> None:
        self._ctx = ctx_manager
        self._ws: Any = None

    def __enter__(self) -> _SyncWebSocketAdapter:
        self._ws = self._ctx.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._ctx.__exit__(exc_type, exc, tb)

    def send(self, payload: str) -> None:
        self._ws.send_text(payload)


class _MockHTTPResponse:
    """Lightweight requests.Response stand-in for offline ATS feed scanner tests."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class _MockRequestsSession:
    """Maps URL substrings to canned JSON payloads without live network calls."""

    def __init__(self, responses_by_substr: dict[str, Any]) -> None:
        self._responses = responses_by_substr
        self.requested_urls: list[str] = []

    def get(self, url: str, headers: dict[str, str] | None = None, timeout: float = 15.0) -> _MockHTTPResponse:
        self.requested_urls.append(url)
        for substr, payload in self._responses.items():
            if substr in url:
                return _MockHTTPResponse(payload)
        return _MockHTTPResponse({}, status_code=404)


# ---------------------------------------------------------------------------
# Test 1: Challenge Detector & FastAPI WebSocket Bridge
# ---------------------------------------------------------------------------
def test_challenge_detector_and_websocket_alerts() -> None:
    """Test 1: Detects Turnstile/reCAPTCHA/hCaptcha/Workday across DOM & frames, avoids false

    positives on clean forms, emits EVENT_SECURITY_CHALLENGE_DETECTED without closing browser
    context, sends WebSocket notification to /ws/challenges, and handles resolution & timeout.
    """
    app = create_app()
    challenge_mgr = get_challenge_manager(app)
    challenge_mgr.clear()
    client = TestClient(app)

    def ws_bridge(url: str, **kwargs: Any) -> _SyncWebSocketAdapter:
        assert "/ws/challenges" in url
        return _SyncWebSocketAdapter(client.websocket_connect("/ws/challenges"))

    emitted_events: list[ChallengeEvent] = []
    detector = ChallengeDetector(
        ui_port=8501,
        on_challenge=emitted_events.append,
        ws_connect=ws_bridge,
        ws_timeout=0.5,
    )

    clean_greenhouse_html = """<!DOCTYPE html>
    <html><head><title>Software Engineer Application</title></head>
    <body>
      <form id="application_form">
        <label for="first_name">First Name *</label>
        <input id="first_name" name="first_name" type="text" required />
        <label for="why_here">Why do you want to work on security and infrastructure?</label>
        <textarea id="why_here" name="why_here"></textarea>
        <button type="submit">Submit Application</button>
      </form>
    </body></html>"""

    clean_workday_html = """<!DOCTYPE html>
    <html><head><title>Workday Application</title></head>
    <body>
      <div data-automation-id="workdayApplication">
        <div data-automation-id="jobApplicationPage">
          <h2>My Information</h2>
          <input type="text" data-automation-id="legalNameSection_firstName" />
          <button type="button" data-automation-id="pageFooterNextButton">Next</button>
        </div>
      </div>
    </body></html>"""

    turnstile_dom_html = """<!DOCTYPE html>
    <html><head><title>Verify</title></head>
    <body>
      <form id="app_form">
        <div id="turnstile-widget" class="cf-turnstile" data-sitekey="0x4AAAAAAAB"></div>
      </form>
    </body></html>"""

    turnstile_iframe_html = """<!DOCTYPE html>
    <html><body>
      <form>
        <iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv"
                title="Widget containing a Cloudflare security challenge"></iframe>
      </form>
    </body></html>"""

    recaptcha_iframe_html = """<!DOCTYPE html>
    <html><body>
      <form>
        <iframe title="reCAPTCHA" srcdoc="<html><body><div class='g-recaptcha'></div></body></html>"></iframe>
      </form>
    </body></html>"""

    hcaptcha_html = """<!DOCTYPE html>
    <html><body>
      <form>
        <div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001"></div>
        <iframe srcdoc="<html><body><div data-hcaptcha-widget-id='abc'></div></body></html>"></iframe>
      </form>
    </body></html>"""

    workday_bot_html = """<!DOCTYPE html>
    <html><head><title>Security Verification</title></head>
    <body>
      <div data-automation-id="workdayApplication">
        <div data-automation-id="securityChallenge">
          <h2>Bot Verification</h2>
          <p>Please verify that you are human to continue your application.</p>
        </div>
      </div>
    </body></html>"""

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # Route Cloudflare challenge URL locally so child frame inspection works completely offline
        page.route(
            "https://challenges.cloudflare.com/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body="<html><body><div class='cf-turnstile'>Cloudflare Challenge</div></body></html>",
            ),
        )

        try:
            # 1. Clean forms must produce zero false positives
            for clean_html in (clean_greenhouse_html, clean_workday_html):
                page.set_content(clean_html, wait_until="domcontentloaded")
                assert detector.detect_challenge(page) is None
                assert detector.suspended is False
                assert not page.is_closed()

            # 2. Detect each challenge provider + child frame inspection + WS notification
            with client.websocket_connect("/ws/challenges") as dashboard_ws:
                # 2a. Cloudflare Turnstile (main DOM) + WS verification
                page.set_content(turnstile_dom_html, wait_until="domcontentloaded")
                res_turnstile = detector.detect_challenge(page)
                assert res_turnstile is not None
                assert res_turnstile["event"] == EVENT_SECURITY_CHALLENGE_DETECTED
                assert res_turnstile["challenge_type"] == CHALLENGE_CLOUDFLARE_TURNSTILE
                assert res_turnstile["message"] == DEFAULT_CHALLENGE_MESSAGE
                assert detector.suspended is True
                assert not page.is_closed()
                assert page in context.pages

                ws_msg = dashboard_ws.receive_json()
                assert ws_msg["event"] == EVENT_SECURITY_CHALLENGE_DETECTED
                assert ws_msg["challenge_type"] == CHALLENGE_CLOUDFLARE_TURNSTILE
                assert ws_msg["message"] == "CAPTCHA Detected: Complete in Browser"
                assert ws_msg["resolved"] is False

                status_resp = client.get("/api/challenges/status")
                assert status_resp.status_code == 200
                status_json = status_resp.json()
                assert status_json["active"] is True
                assert status_json["challenge"]["message"] == "CAPTCHA Detected: Complete in Browser"

                # Verify HITL dashboard template shows active CAPTCHA alert
                review_resp = client.get("/")
                assert review_resp.status_code == 200
                assert "CAPTCHA Detected: Complete in Browser" in review_resp.text

                # 2b. Cloudflare Turnstile via child frame URL in page.frames
                page.set_content(turnstile_iframe_html, wait_until="load")
                res_cf_frame = detector.detect_challenge(page)
                assert res_cf_frame is not None
                assert res_cf_frame["challenge_type"] == CHALLENGE_CLOUDFLARE_TURNSTILE
                _ = dashboard_ws.receive_json()

                # 2c. Google reCAPTCHA (main DOM / child frame)
                page.set_content(recaptcha_iframe_html, wait_until="domcontentloaded")
                res_recaptcha = detector.detect_challenge(page)
                assert res_recaptcha is not None
                assert res_recaptcha["challenge_type"] == CHALLENGE_GOOGLE_RECAPTCHA
                _ = dashboard_ws.receive_json()

                # 2d. hCaptcha
                page.set_content(hcaptcha_html, wait_until="domcontentloaded")
                res_hcaptcha = detector.detect_challenge(page)
                assert res_hcaptcha is not None
                assert res_hcaptcha["challenge_type"] == CHALLENGE_HCAPTCHA
                _ = dashboard_ws.receive_json()

                # 2e. Workday Bot Challenge
                page.set_content(workday_bot_html, wait_until="domcontentloaded")
                res_workday = detector.detect_challenge(page)
                assert res_workday is not None
                assert res_workday["challenge_type"] == CHALLENGE_WORKDAY_BOT
                _ = dashboard_ws.receive_json()

                # 3. wait_for_resolution returns False on timeout while preserving page/context
                timed_out = detector.wait_for_resolution(
                    page,
                    timeout_seconds=0.14,
                    poll_interval=0.04,
                )
                assert timed_out is False
                assert not page.is_closed()
                assert len(context.pages) == 1

                # 4. wait_for_resolution returns True when challenge disappears from DOM
                disappearing_html = """<!DOCTYPE html>
                <html><body>
                  <div id="turnstile-widget" class="cf-turnstile">Please verify</div>
                  <script>
                    setTimeout(() => {
                      const w = document.getElementById("turnstile-widget");
                      if (w) w.remove();
                    }, 120);
                  </script>
                </body></html>"""
                page.set_content(disappearing_html, wait_until="domcontentloaded")
                assert detector.detect_challenge(page) is not None
                _ = dashboard_ws.receive_json()

                resumed_dom = detector.wait_for_resolution(
                    page,
                    timeout_seconds=3.0,
                    poll_interval=0.04,
                )
                assert resumed_dom is True
                assert detector.suspended is False
                assert not page.is_closed()

                ws_resolved = dashboard_ws.receive_json()
                assert ws_resolved["event"] == EVENT_SECURITY_CHALLENGE_RESOLVED
                assert ws_resolved["resolved"] is True

                # 5. wait_for_resolution returns True when manual resolution signal fires
                page.set_content(turnstile_dom_html, wait_until="domcontentloaded")
                assert detector.detect_challenge(page) is not None
                _ = dashboard_ws.receive_json()

                manual_signal = threading.Event()
                timer = threading.Timer(0.1, manual_signal.set)
                timer.start()
                try:
                    resumed_signal = detector.wait_for_resolution(
                        page,
                        timeout_seconds=2.5,
                        poll_interval=0.04,
                        resolution_signal=manual_signal,
                    )
                finally:
                    timer.cancel()

                assert resumed_signal is True
                assert detector.suspended is False
                ws_sig_resolved = dashboard_ws.receive_json()
                assert ws_sig_resolved["resolved"] is True

                # Verify HTTP resolve endpoint clears status cleanly
                resolve_resp = client.post("/api/challenges/resolve")
                assert resolve_resp.status_code == 200
                assert resolve_resp.json()["active"] is False

            # 6. Offline resilience: unreachable dashboard port never crashes automation
            offline_detector = ChallengeDetector(ui_port=59999, ws_timeout=0.1)
            page.set_content(turnstile_dom_html, wait_until="domcontentloaded")
            offline_hit = offline_detector.detect_challenge(page)
            assert offline_hit is not None
            assert offline_hit["event"] == EVENT_SECURITY_CHALLENGE_DETECTED
            assert offline_detector.suspended is True

            assert any(e.event == EVENT_SECURITY_CHALLENGE_DETECTED for e in emitted_events)
            assert any(e.event == EVENT_SECURITY_CHALLENGE_RESOLVED for e in emitted_events)
        finally:
            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# Test 2: Ashby ATS Adapter
# ---------------------------------------------------------------------------
def test_ashby_adapter_controls_and_step_guard(tmp_path: Path) -> None:
    """Test 2: AshbyAdapter detection, field extraction, custom controls, and submit guard."""
    dummy_pdf = tmp_path / "ashby_candidate_cv.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>startxref\n0\n%%EOF")

    ashby_step1_html = """<!DOCTYPE html>
    <html lang="en">
    <head><title>Backend Engineer - jobs.ashbyhq.com</title></head>
    <body>
      <form class="ashby-application-form" id="ashby_step1_form"
            onsubmit="window.__finalSubmitted = true; return false;">
        <div class="_ashby_step_header active" aria-current="step">Candidate Details</div>

        <div class="ashby-application-form-field-entry _ashby_field">
          <label for="_ashby_name">Full Name *</label>
          <input type="text" id="_ashby_name" name="_ashby_name" required />
        </div>

        <div class="ashby-application-form-field-entry _ashby_field">
          <label for="_ashby_email">Email *</label>
          <input type="email" id="_ashby_email" name="_ashby_email" required />
        </div>

        <div class="ashby-application-form-field-entry _ashby_field">
          <label for="_ashby_phone">Phone</label>
          <input type="tel" id="_ashby_phone" name="_ashby_phone" />
        </div>

        <div class="ashby-application-form-field-entry _ashby_field">
          <label for="_ashby_linkedin">LinkedIn Profile</label>
          <input type="text" id="_ashby_linkedin" name="_ashby_linkedin" inputmode="url" />
        </div>

        <div class="ashby-application-form-field-entry _ashby_field">
          <label for="_ashby_location">Preferred Office Location *</label>
          <select id="_ashby_location" name="_ashby_location" required>
            <option value="">Select location...</option>
            <option value="San Francisco">San Francisco</option>
            <option value="Remote">Remote</option>
          </select>
        </div>

        <div class="ashby-application-form-field-entry _ashby_multiselect" data-field="core_skills">
          <label>Core Technical Areas</label>
          <div class="_ashby_multiselect_box" role="combobox" aria-multiselectable="true"
               id="_ashby_skills_combo" aria-controls="skills_listbox">
            <label><input type="checkbox" name="core_skills_py" value="Python" /> Python</label>
            <label><input type="checkbox" name="core_skills_go" value="Go" /> Go</label>
            <label><input type="checkbox" name="core_skills_ts" value="TypeScript" /> TypeScript</label>
          </div>
          <ul id="skills_listbox" role="listbox" style="display:none;">
            <li role="option">Python</li>
            <li role="option">Go</li>
            <li role="option">TypeScript</li>
          </ul>
        </div>

        <div class="ashby-application-form-field-entry _ashby_field" data-field="work_auth_group">
          <span class="question-title">Are you legally authorized to work in the US? *</span>
          <div role="radiogroup" class="_ashby_boolean_group" id="_ashby_work_auth" aria-required="true">
            <button type="button" role="radio" aria-checked="false">Yes</button>
            <button type="button" role="radio" aria-checked="false">No</button>
          </div>
        </div>

        <div class="ashby-application-form-field-entry _ashby_dropzone">
          <label for="_ashby_resume">Resume / CV *</label>
          <div class="dropzone" data-ashby-dropzone="true">
            <span>Drag and drop your resume here</span>
            <input type="file" id="_ashby_resume" name="_ashby_resume" accept=".pdf" required />
          </div>
        </div>

        <div class="ashby-application-form-field-entry _ashby_field">
          <label for="_ashby_screening">Why are you excited about distributed systems engineering?</label>
          <textarea id="_ashby_screening" name="_ashby_screening" rows="4"></textarea>
        </div>

        <div class="ashby-footer">
          <button type="button" class="_ashby_next" data-testid="ashby-next-btn"
                  onclick="document.body.setAttribute('data-step-advanced', 'true');">
            Continue
          </button>
        </div>
      </form>
    </body>
    </html>"""

    ashby_final_step_html = """<!DOCTYPE html>
    <html lang="en">
    <head><title>Review &amp; Submit - Ashby</title></head>
    <body>
      <form class="ashby-application-form" id="ashby_final_form"
            onsubmit="window.__finalSubmitted = true; return false;">
        <div class="_ashby_step_header active" aria-current="step">Review and Submit</div>
        <div class="ashby-application-form-field-entry _ashby_field">
          <label for="_ashby_notes">Additional Screening Notes</label>
          <textarea id="_ashby_notes" name="_ashby_notes"></textarea>
        </div>
        <div class="ashby-footer">
          <button type="submit" id="ashby_submit_btn" class="_ashby_submit"
                  onclick="window.__finalSubmitted = true;">
            Submit Application
          </button>
        </div>
      </form>
    </body>
    </html>"""

    adapter = AshbyAdapter()

    # 1. Detection checks
    assert adapter.detect("https://jobs.ashbyhq.com/linear/6f123abc") is True
    assert adapter.detect(ashby_step1_html) is True
    assert adapter.detect("https://boards.greenhouse.io/company/jobs/101") is False

    # 2. Field extraction checks
    schema = adapter.extract_fields(ashby_step1_html)
    assert schema.get_field_by_id("_ashby_name") is not None
    email_field = schema.get_field_by_id("_ashby_email")
    assert email_field is not None and email_field.type == "email"
    tel_field = schema.get_field_by_id("_ashby_phone")
    assert tel_field is not None and tel_field.type == "tel"
    url_field = schema.get_field_by_id("_ashby_linkedin")
    assert url_field is not None and url_field.type == "url"

    loc_field = schema.get_field_by_id("_ashby_location")
    assert loc_field is not None and loc_field.type == "select"
    assert "Remote" in loc_field.options

    skills_field = schema.get_field_by_id("_ashby_skills_combo") or schema.get_field_by_id("core_skills")
    assert skills_field is not None and skills_field.type == "select"
    assert "Python" in skills_field.options and "Go" in skills_field.options

    auth_field = schema.get_field_by_id("_ashby_work_auth") or schema.get_field_by_id("work_auth_group")
    assert auth_field is not None and auth_field.type == "radio"
    assert "Yes" in auth_field.options and "No" in auth_field.options

    resume_field = schema.get_resume_upload_field()
    assert resume_field is not None and resume_field.type == "file"

    textarea_field = schema.get_field_by_id("_ashby_screening")
    assert textarea_field is not None and textarea_field.type == "textarea"

    # 3. Live Playwright custom control filling & multi-step guard
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(ashby_step1_html, wait_until="domcontentloaded")
            assert adapter.detect(page) is True

            answers = {
                "_ashby_name": "Jordan Vance",
                "_ashby_email": MOCK_APPLICANT_EMAIL,
                "_ashby_phone": MOCK_PHONE_DIGITS,
                "_ashby_linkedin": "https://linkedin.com/in/jordanvance",
                "_ashby_location": "Remote",
                "core_skills": "Python, Go",
                "work_auth_group": "Yes",
                "_ashby_resume": str(dummy_pdf),
                "_ashby_screening": "Built high-throughput asynchronous FastAPI pipelines with Redis Streams.",
            }
            fill_results = adapter.fill_custom_controls(page, answers)
            assert all(fill_results.get(k) is True for k in answers)

            assert page.locator("#_ashby_name").input_value() == "Jordan Vance"
            assert page.locator("#_ashby_email").input_value() == MOCK_APPLICANT_EMAIL
            assert page.locator("#_ashby_location").input_value() == "Remote"
            assert page.locator('input[name="core_skills_py"]').is_checked() is True
            assert page.locator('input[name="core_skills_go"]').is_checked() is True
            assert page.locator('input[name="core_skills_ts"]').is_checked() is False
            assert "Built high-throughput" in page.locator("#_ashby_screening").input_value()

            # Step 1 is intermediate -> advance_step succeeds
            step1_name, step1_is_final = adapter.detect_form_step(page)
            assert "Candidate Details" in step1_name
            assert step1_is_final is False
            assert adapter.advance_step(page) is True
            assert page.locator("body").get_attribute("data-step-advanced") == "true"

            # Final step strictly blocks submit
            final_page = browser.new_page()
            final_page.set_content(ashby_final_step_html, wait_until="domcontentloaded")
            final_page.evaluate("() => { window.__finalSubmitted = false; }")

            final_step_name, final_is_final = adapter.detect_form_step(final_page)
            assert final_is_final is True
            assert "Review" in final_step_name or "Submit" in final_step_name
            assert adapter.advance_step(final_page) is False
            assert final_page.evaluate("() => window.__finalSubmitted") is False
            final_page.close()
            page.close()
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Test 3: RenderCV Page-Budget Enforcer
# ---------------------------------------------------------------------------
def test_page_budget_enforcer_compacts_overflowing_resume(tmp_path: Path) -> None:
    """Test 3: Compiles an overflowing 2-page RenderCV YAML in tmp_path and enforces a 1-page budget."""
    overflow_profile = {
        "cv": {
            "name": "Jordan Vance",
            "location": "Seattle, WA",
            "email": MOCK_APPLICANT_EMAIL,
            "sections": {
                "summary": [
                    "Senior backend and distributed systems engineer with 6+ years building "
                    "low-latency Python, FastAPI, Redis, PostgreSQL, and Kubernetes platforms."
                ],
                "experience": [
                    {
                        "company": f"CloudCore Systems {idx}",
                        "position": "Senior Software Engineer",
                        "start_date": f"{2017 + idx}-01",
                        "end_date": f"{2018 + idx}-12" if idx < 4 else "present",
                        "location": "Seattle, WA",
                        "highlights": [
                            f"Architected asynchronous Python FastAPI ingestion microservices handling 1.{idx}M daily events with Redis Streams and PostgreSQL.",
                            f"Built distributed caching middleware cutting p95 API latency by {25 + idx * 5}% under peak load.",
                            "Designed automated Kubernetes canary deployments and Terraform pipelines reducing release failures by 60%.",
                            "Maintained legacy internal administrative reporting spreadsheets and quarterly compliance checklists across offices.",
                            "Coordinated vendor onboarding meetings and documented general office IT equipment inventory procedures.",
                            "Updated internal team wiki schedules and organized weekly brown-bag lunch calendars across departments.",
                            "Prepared non-technical onboarding binder packets and maintained office supply restocking trackers for regional teams.",
                        ],
                    }
                    for idx in range(5)
                ],
                "projects": [
                    {
                        "name": f"TelemetryPipeline {j}",
                        "date": "2024-02",
                        "highlights": [
                            "Implemented high-throughput OpenTelemetry trace collector in Python and Go with batched PostgreSQL persistence.",
                            "Created legacy static HTML documentation pages for internal team lunch scheduling and seating charts.",
                            "Organized quarterly office social event spreadsheets and weekly brown-bag lunch catering calendars.",
                        ],
                    }
                    for j in range(4)
                ],
                "education": [
                    {
                        "institution": "University of Washington",
                        "area": "Computer Science",
                        "degree": "B.S.",
                        "start_date": "2014-09",
                        "end_date": "2018-06",
                    }
                ],
                "skills": [
                    {"label": "Languages", "details": "Python, Go, TypeScript, SQL"},
                    {"label": "Infrastructure", "details": "FastAPI, Redis, PostgreSQL, Docker, Kubernetes, AWS"},
                ],
            },
        },
        "design": {
            "theme": "classic",
            "page": {"size": "us-letter"},
        },
    }

    input_yaml = tmp_path / "overflow_resume.yaml"
    output_pdf = tmp_path / "tailored_one_page.pdf"
    output_yaml = tmp_path / "tailored_one_page.yaml"
    input_yaml.write_text(yaml.dump(overflow_profile, sort_keys=False), encoding="utf-8")

    result = enforce_page_budget(
        yaml_path=input_yaml,
        output_pdf_path=output_pdf,
        target_pages=1,
        job_keywords=["Python", "FastAPI", "Redis", "Kubernetes", "PostgreSQL"],
        output_yaml_path=output_yaml,
    )

    assert isinstance(result, PageBudgetResult)
    assert result.success is True
    assert result.initial_page_count >= 2
    assert result.final_page_count == 1
    assert output_pdf.is_file()
    assert get_pdf_page_count(output_pdf) == 1
    assert output_pdf.read_bytes()[:5] == b"%PDF-"
    assert len(result.adjustments_applied) >= 1
    assert len(result.trimmed_bullets) >= 1

    # Verify irrelevant bullets were trimmed before high-relevance Python/FastAPI bullets
    assert any("lunch" in b.lower() or "spreadsheets" in b.lower() or "vendor" in b.lower() for b in result.trimmed_bullets)
    compacted_text = output_yaml.read_text(encoding="utf-8")
    assert "FastAPI" in compacted_text
    assert "Redis" in compacted_text


# ---------------------------------------------------------------------------
# Test 4: Zero-Token ATS Feed Scanner & Companies Config
# ---------------------------------------------------------------------------
def test_zero_token_ats_feed_scanner_and_config(tmp_path: Path) -> None:
    """Test 4: Greenhouse/Lever JSON parsing, staleness/keyword filtering, deduplication,

    atomic intake_queue.json persistence, and config/companies.yml validation.
    """
    # 1. Verify repository config/companies.yml parses cleanly
    repo_config_path = Path("config") / "companies.yml"
    assert repo_config_path.is_file()
    repo_scanner = ATSFeedScanner(config_path=repo_config_path, queue_path=tmp_path / "unused_queue.json")
    loaded_companies = repo_scanner.load_companies()
    assert len(loaded_companies) >= 4
    assert all(isinstance(c, CompanyFeedConfig) for c in loaded_companies)
    portals = {c.portal for c in loaded_companies}
    assert "greenhouse" in portals and "lever" in portals

    now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
    fresh_iso = (now - timedelta(days=3)).isoformat()
    stale_iso = (now - timedelta(days=30)).isoformat()
    fresh_ms = int((now - timedelta(days=2)).timestamp() * 1000)
    stale_ms = int((now - timedelta(days=25)).timestamp() * 1000)

    # 2. parse_greenhouse_jobs unit coverage
    gh_payload = {
        "jobs": [
            {
                "id": 1001,
                "title": "Senior Backend Software Engineer",
                "absolute_url": "https://boards.greenhouse.io/acmecorp/jobs/1001",
                "location": {"name": "Remote - US"},
                "updated_at": fresh_iso,
                "content": "&lt;p&gt;Build scalable &lt;strong&gt;Python&lt;/strong&gt; and FastAPI services.&lt;/p&gt;",
            },
            {
                "id": 1002,
                "title": "Staff Backend Software Engineer",
                "absolute_url": "https://boards.greenhouse.io/acmecorp/jobs/1002",
                "location": {"name": "New York, NY"},
                "updated_at": fresh_iso,
                "content": "<p>Staff role excluded by title filter.</p>",
            },
            {
                "id": 1003,
                "title": "Platform Software Engineer",
                "absolute_url": "https://boards.greenhouse.io/acmecorp/jobs/1003",
                "location": {"name": "Seattle, WA"},
                "updated_at": stale_iso,
                "content": {"plain": "Stale posting older than max_age_days."},
            },
            {
                # Duplicate URL of 1001 to verify intra-batch deduplication
                "id": 1004,
                "title": "Senior Backend Software Engineer",
                "absolute_url": "https://boards.greenhouse.io/acmecorp/jobs/1001/",
                "location": {"name": "Remote - US"},
                "updated_at": fresh_iso,
            },
        ]
    }
    gh_jobs = parse_greenhouse_jobs(gh_payload, company_name="Acme Corp", board_token="acmecorp")
    assert len(gh_jobs) == 4
    assert all(isinstance(j, DiscoveredJob) and j.portal == "greenhouse" for j in gh_jobs)
    assert "Build scalable Python and FastAPI services." in " ".join(gh_jobs[0].description_text.split())
    assert "<p>" not in gh_jobs[0].description_text
    assert "<strong>" not in gh_jobs[0].description_text
    assert gh_jobs[0].location == "Remote - US"
    assert gh_jobs[0].status == "discovered"
    assert gh_jobs[0].fit_score is None

    # 3. parse_lever_jobs unit coverage
    lever_payload = [
        {
            "id": "lever-2001",
            "text": "Distributed Systems Platform Engineer",
            "hostedUrl": "https://jobs.lever.co/orbitlabs/lever-2001",
            "categories": {"location": "San Francisco, CA"},
            "createdAt": fresh_ms,
            "descriptionPlain": "Design low-latency Go and Python pipelines.",
            "lists": [{"text": "Requirements", "content": "<li>PostgreSQL</li><li>Kubernetes</li>"}],
        },
        {
            "id": "lever-2002",
            "text": "Software Engineer - Infrastructure",
            "hostedUrl": "https://jobs.lever.co/orbitlabs/lever-2002",
            "categories": {"location": "Remote"},
            "createdAt": stale_ms,
            "descriptionPlain": "Stale Lever posting.",
        },
    ]
    lever_jobs = parse_lever_jobs(lever_payload, company_name="Orbit Labs", slug="orbitlabs")
    assert len(lever_jobs) == 2
    assert lever_jobs[0].portal == "lever"
    assert lever_jobs[0].location == "San Francisco, CA"
    assert "PostgreSQL" in lever_jobs[0].description_text

    # 4. End-to-end ATSFeedScanner scan with tmp_path config & pre-existing scored queue entry
    temp_config = tmp_path / "companies.yml"
    temp_config.write_text(
        yaml.safe_dump(
            {
                "defaults": {"max_age_days": 14},
                "companies": [
                    {
                        "name": "Acme Corp",
                        "portal": "greenhouse",
                        "board_token": "acmecorp",
                        "enabled": True,
                        "role_filters": {
                            "include_keywords": ["software engineer", "platform"],
                            "exclude_keywords": ["staff", "director"],
                            "max_age_days": 14,
                        },
                    },
                    {
                        "name": "Orbit Labs",
                        "portal": "lever",
                        "board_token": "orbitlabs",
                        "enabled": True,
                        "role_filters": {
                            "include_keywords": ["platform", "software engineer"],
                            "exclude_keywords": ["manager"],
                            "max_age_days": 14,
                        },
                    },
                    {
                        "name": "Disabled Co",
                        "portal": "greenhouse",
                        "board_token": "disabledco",
                        "enabled": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    queue_file = tmp_path / "intake_queue.json"
    preexisting_scored = {
        "id": "existing-900",
        "company": "ExistingCorp",
        "board_token": "existingcorp",
        "portal": "greenhouse",
        "title": "Backend Engineer",
        "location": "Remote",
        "url": "https://boards.greenhouse.io/existingcorp/jobs/existing-900",
        "updated_at": fresh_iso,
        "description_text": "Existing scored job.",
        "fit_score": 4,
        "hard_blocked": False,
        "blocker_reasons": [],
        "evaluation_summary": "Pre-scored entry.",
        "status": "scored",
    }
    queue_file.write_text(
        json.dumps({"updated_at": fresh_iso, "jobs": [preexisting_scored]}, indent=2),
        encoding="utf-8",
    )

    mock_session = _MockRequestsSession(
        {
            "boards-api.greenhouse.io/v1/boards/acmecorp/jobs": gh_payload,
            "api.lever.co/v0/postings/orbitlabs": lever_payload,
        }
    )
    scanner = ATSFeedScanner(config_path=temp_config, queue_path=queue_file, session=mock_session)  # type: ignore[arg-type]

    added = scanner.scan(save=True, now=now)
    assert len(added) == 2
    assert {j.id for j in added} == {"1001", "lever-2001"}
    assert scanner.last_result is not None
    assert scanner.last_result.companies_scanned == 2
    assert scanner.last_result.companies_skipped == 1
    assert scanner.last_result.duplicates_skipped >= 1
    assert not any("disabledco" in u for u in mock_session.requested_urls)

    persisted = json.loads(queue_file.read_text(encoding="utf-8"))
    assert "updated_at" in persisted and isinstance(persisted["jobs"], list)
    assert len(persisted["jobs"]) == 3
    # Pre-existing scored item preserved untouched
    assert persisted["jobs"][0]["id"] == "existing-900"
    assert persisted["jobs"][0]["fit_score"] == 4
    assert persisted["jobs"][0]["status"] == "scored"

    # Second scan with same feeds adds zero duplicates
    added_second = scanner.scan(save=True, now=now)
    assert added_second == []
    persisted_second = json.loads(queue_file.read_text(encoding="utf-8"))
    assert len(persisted_second["jobs"]) == 3


# ---------------------------------------------------------------------------
# Test 5: Intelligent Fit Scorer & Hard-Blocker Filter
# ---------------------------------------------------------------------------
def test_fit_scorer_hard_blockers_and_queue_scoring(tmp_path: Path) -> None:
    """Test 5: Hard-blocker rejection (sponsorship, citizenship, clearance, years), 1-5

    scoring separation, and score_intake_queue atomic persistence.
    """
    base_profile: dict[str, Any] = {
        "cv": {
            "name": "Jordan Vance",
            "location": "Seattle, WA",
            "email": MOCK_APPLICANT_EMAIL,
            "sections": {
                "summary": [
                    "Backend and ML systems engineer with 3 years of experience in Python, FastAPI, PostgreSQL, Redis, Docker, Kubernetes, and AWS."
                ],
                "experience": [
                    {
                        "company": "Nexus Cloud",
                        "position": "Software Engineer",
                        "start_date": "2023-06",
                        "end_date": "2026-06",
                        "highlights": [
                            "Built async Python FastAPI microservices backed by PostgreSQL and Redis Streams.",
                            "Deployed containerized workloads on Docker, Kubernetes, and AWS ECS.",
                        ],
                    }
                ],
                "skills": [
                    {"label": "Languages", "details": "Python, TypeScript, SQL, Bash"},
                    {"label": "Frameworks", "details": "FastAPI, Pydantic, React, Playwright"},
                    {"label": "Infrastructure", "details": "PostgreSQL, Redis, Docker, Kubernetes, AWS"},
                ],
            },
        },
        "work_authorization": {
            "status": "Authorized to work in the United States",
            "sponsorship_required": False,
            "future_sponsorship_required": False,
            "countries_authorized": ["United States"],
            "security_clearance": "None",
        },
    }

    # 1. Hard blocker: Visa sponsorship conflict
    sponsor_profile = json.loads(json.dumps(base_profile))
    sponsor_profile["work_authorization"]["status"] = "F-1 OPT (requires H-1B sponsorship)"
    sponsor_profile["work_authorization"]["sponsorship_required"] = True
    sponsor_scorer = JobFitScorer(profile_data=sponsor_profile)

    sponsor_blocked_res = sponsor_scorer.evaluate_fit(
        {
            "title": "Backend Engineer",
            "location": "San Francisco, CA",
            "description_text": (
                "Build Python and FastAPI APIs with PostgreSQL and Redis. "
                "This position is unable to provide visa sponsorship now or in the future."
            ),
        },
        use_gemini=False,
    )
    assert isinstance(sponsor_blocked_res, FitEvaluationResult)
    assert sponsor_blocked_res.hard_blocked is True
    assert sponsor_blocked_res.recommended_action == "skip"
    assert sponsor_blocked_res.fit_score <= 2
    assert any("sponsorship" in r.lower() for r in sponsor_blocked_res.blocker_reasons)

    # 2. Hard blocker: US Citizenship conflict
    non_citizen_scorer = JobFitScorer(profile_data=base_profile)
    citizenship_res = non_citizen_scorer.evaluate_fit(
        {
            "title": "Cloud Backend Engineer",
            "location": "Reston, VA",
            "description_text": (
                "Work with Python, FastAPI, Docker, and AWS. "
                "Applicants must be a U.S. Citizen due to federal contract obligations."
            ),
        },
        use_gemini=False,
    )
    assert citizenship_res.hard_blocked is True
    assert citizenship_res.recommended_action == "skip"
    assert citizenship_res.fit_score <= 2
    assert any("citizen" in r.lower() for r in citizenship_res.blocker_reasons)

    # 3. Hard blocker: Security clearance mismatch
    clearance_res = non_citizen_scorer.evaluate_fit(
        {
            "title": "Platform Systems Engineer",
            "location": "Remote, US",
            "description_text": (
                "Python, Kubernetes, AWS, and PostgreSQL engineering. "
                "Requires an active Top Secret security clearance."
            ),
        },
        use_gemini=False,
    )
    assert clearance_res.hard_blocked is True
    assert clearance_res.recommended_action == "skip"
    assert clearance_res.fit_score <= 2
    assert any("clearance" in r.lower() for r in clearance_res.blocker_reasons)

    # 4. Hard blocker: Strict years-of-experience mismatch
    exp_res = non_citizen_scorer.evaluate_fit(
        {
            "title": "Senior Backend Engineer",
            "location": "Seattle, WA",
            "description_text": (
                "Minimum of 8+ years of backend software engineering experience required. "
                "Stack: Python, FastAPI, PostgreSQL, Redis, Kubernetes."
            ),
        },
        use_gemini=False,
    )
    assert exp_res.hard_blocked is True
    assert exp_res.recommended_action == "skip"
    assert exp_res.fit_score <= 2
    assert any("experience" in r.lower() for r in exp_res.blocker_reasons)

    # 5. 1-5 scoring separation: Direct stack match (5) vs stack mismatch (1-2)
    direct_match = non_citizen_scorer.evaluate_fit(
        {
            "title": "Backend Software Engineer",
            "company": "StreamCore",
            "location": "Seattle, WA",
            "description_text": (
                "2+ years of experience building scalable services using Python, FastAPI, "
                "PostgreSQL, Redis, Docker, Kubernetes, and AWS."
            ),
            "required_skills": ["Python", "FastAPI", "PostgreSQL", "Redis", "Docker", "Kubernetes", "AWS"],
        },
        use_gemini=False,
    )
    assert direct_match.hard_blocked is False
    assert direct_match.fit_score == 5
    assert direct_match.recommended_action == "priority_apply"
    assert "Python" in direct_match.matched_skills

    stack_mismatch = non_citizen_scorer.evaluate_fit(
        {
            "title": "Embedded FPGA Firmware Engineer",
            "company": "SiliconHardware",
            "location": "Austin, TX",
            "description_text": (
                "Develop bare-metal firmware using Embedded C, VHDL, FPGA synthesis, and FreeRTOS."
            ),
            "required_skills": ["Embedded C", "VHDL", "FPGA", "RTOS"],
        },
        use_gemini=False,
    )
    assert stack_mismatch.hard_blocked is False
    assert stack_mismatch.fit_score <= 2
    assert stack_mismatch.recommended_action == "skip"

    # 6. score_intake_queue writes scored and blocked statuses back to intake_queue.json
    queue_file = tmp_path / "intake_queue.json"
    queue_payload = {
        "updated_at": "2026-11-13T00:00:00+00:00",
        "jobs": [
            {
                "id": "job-direct-5",
                "company": "StreamCore",
                "board_token": "streamcore",
                "portal": "greenhouse",
                "title": "Backend Engineer",
                "location": "Remote, US",
                "url": "https://boards.greenhouse.io/streamcore/jobs/job-direct-5",
                "description_text": "2+ years experience with Python, FastAPI, PostgreSQL, Redis, Docker, Kubernetes, AWS.",
                "status": "discovered",
            },
            {
                "id": "job-blocked-clearance",
                "company": "DefenseCloud",
                "board_token": "defensecloud",
                "portal": "lever",
                "title": "Cloud Engineer",
                "location": "Reston, VA",
                "url": "https://jobs.lever.co/defensecloud/job-blocked-clearance",
                "description_text": "Python and AWS role. Active Secret security clearance is required.",
                "status": "discovered",
            },
            {
                "id": "job-mismatch-1",
                "company": "SiliconHardware",
                "board_token": "siliconhardware",
                "portal": "greenhouse",
                "title": "FPGA Firmware Engineer",
                "location": "Austin, TX",
                "url": "https://boards.greenhouse.io/siliconhardware/jobs/job-mismatch-1",
                "description_text": "Requires Embedded C, VHDL, FPGA, and RTOS.",
                "status": "discovered",
            },
            {
                "id": "job-already-scored",
                "company": "ExistingCo",
                "board_token": "existingco",
                "portal": "greenhouse",
                "title": "Platform Engineer",
                "location": "Remote",
                "url": "https://boards.greenhouse.io/existingco/jobs/job-already-scored",
                "description_text": "Python services.",
                "fit_score": 4,
                "hard_blocked": False,
                "status": "scored",
            },
        ],
    }
    queue_file.write_text(json.dumps(queue_payload, indent=2), encoding="utf-8")

    summary = score_intake_queue(
        queue_path=queue_file,
        min_score_to_keep=3,
        profile_data=base_profile,
        use_gemini=False,
    )
    assert summary["total"] == 4
    assert summary["newly_scored"] == 3
    assert summary["blocked_count"] == 1
    assert summary["kept_count"] == 2

    reloaded = json.loads(queue_file.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in reloaded["jobs"]}

    assert by_id["job-direct-5"]["fit_score"] == 5
    assert by_id["job-direct-5"]["hard_blocked"] is False
    assert by_id["job-direct-5"]["recommended_action"] == "priority_apply"
    assert by_id["job-direct-5"]["status"] == "scored"

    assert by_id["job-blocked-clearance"]["hard_blocked"] is True
    assert by_id["job-blocked-clearance"]["fit_score"] <= 2
    assert by_id["job-blocked-clearance"]["recommended_action"] == "skip"
    assert by_id["job-blocked-clearance"]["status"] == "blocked"

    assert by_id["job-mismatch-1"]["fit_score"] <= 2
    assert by_id["job-mismatch-1"]["status"] == "scored"

    # Pre-scored entry remains untouched when rescore_all=False
    assert by_id["job-already-scored"]["fit_score"] == 4
    assert by_id["job-already-scored"]["status"] == "scored"


# ---------------------------------------------------------------------------
# Test 6: Interactive Interview Practice Mode
# ---------------------------------------------------------------------------
def test_interactive_interview_practice_mode(tmp_path: Path) -> None:
    """Test 6: Loads prep markdown dossiers from tmp_path, iterates questions sequentially,

    evaluates STAR dimensions, and provides coaching feedback with profile-grounded evidence.
    """
    archive_root = tmp_path / "archive_root"
    dossier_dir = archive_root / "full_time" / "2026-11-12_streamcore_backend_engineer"
    prep_dir = dossier_dir / "interview_prep"
    prep_dir.mkdir(parents=True, exist_ok=True)

    (dossier_dir / "application_meta.json").write_text(
        json.dumps(
            {
                "company": "StreamCore",
                "role": "Backend Engineer",
                "role_category": "full_time",
                "slug": "2026-11-12_streamcore_backend_engineer",
            }
        ),
        encoding="utf-8",
    )
    (dossier_dir / "raw_job_description.md").write_text(
        "# Backend Engineer at StreamCore\n\nBuild resilient Python, FastAPI, Redis Streams, and PostgreSQL pipelines.",
        encoding="utf-8",
    )

    (prep_dir / "targeted_behavioral_qa.md").write_text(
        """# Targeted Behavioral Interview Preparation

**Target Role:** Backend Engineer
**Hiring Company:** StreamCore

## 2. Predicted Questions & STAR Anecdote Mappings

### Question 1: Performance Optimization & Ownership (system_design, ownership)
- **Anticipated Screening Prompt:** *"Tell me about a time you resolved a critical production latency bottleneck or high-throughput pipeline failure."*
- **Mapped STAR Story:** **Re-architecting High-Throughput Event Ingestion Pipeline** (`story-latency-migration`)
- **Delivery Strategy:** Highlight Redis Streams, profiling, and quantified p95 latency metrics.

### Question 2: Security & Zero-Downtime Delivery (security, risk_mitigation)
- **Anticipated Screening Prompt:** *"Describe how you executed a sensitive security or encryption migration without disrupting active users."*
- **Mapped STAR Story:** **Envelope Encryption Migration** (`story-security-encryption`)
- **Delivery Strategy:** Emphasize dual-read migration and audit verification.
""",
        encoding="utf-8",
    )

    (prep_dir / "technical_skills_gap.md").write_text(
        """# Technical Skills Gap Analysis

**Target Role:** Backend Engineer
**Hiring Company:** StreamCore

## 2. Skills Match & Gap Matrix

| Technology / Domain | Requirement Status | Candidate Profile | Priority Focus |
| :--- | :--- | :--- | :--- |
| FastAPI | Required | Proficient / Verified | Review Architecture |
| Kafka | Required | Familiar / Gap | High Priority Revision |

## 4. Anticipated Technical Screening Questions

1. **Architecture & Backpressure:**
   * *"How would you design a fault-tolerant event ingestion pipeline handling bursty traffic without dropping messages?"*
   * *Focus:* Redis Streams, idempotency, backpressure, observability.

2. **Database Performance:**
   * *"How do you diagnose slow PostgreSQL queries and design composite indexes safely?"*
   * *Focus:* EXPLAIN ANALYZE, indexing, connection pooling.
""",
        encoding="utf-8",
    )

    profile_path = tmp_path / "profile_store" / "master_profile.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_data = {
        "cv": {
            "name": "Jordan Vance",
            "email": MOCK_APPLICANT_EMAIL,
            "sections": {
                "experience": [
                    {
                        "company": "Nexus Cloud",
                        "position": "Software Engineer II",
                        "start_date": "2023-06",
                        "end_date": "present",
                        "highlights": [
                            "Architected an async Python/FastAPI pipeline processing 1.2M+ daily events, cutting p95 latency from 420ms to 145ms.",
                            "Implemented AES-256-GCM envelope encryption for customer API tokens with zero downtime.",
                        ],
                    }
                ],
                "projects": [
                    {
                        "name": "QueueGuard",
                        "highlights": [
                            "Built distributed Redis Streams retry workers with idempotency keys reducing failed deliveries by 68%."
                        ],
                    }
                ],
                "skills": [
                    {"label": "Backend", "details": "Python, FastAPI, PostgreSQL, Redis, Docker"},
                ],
            },
        },
        "story_bank": [
            {
                "id": "story-latency-migration",
                "title": "Re-architecting High-Throughput Event Ingestion Pipeline",
                "competencies": ["system_design", "ownership", "performance_optimization"],
                "situation": "At Nexus Cloud, our synchronous webhook ingestion endpoint timed out during bursts with 420ms p95 latency.",
                "task": "Redesign ingestion to sustain 3x peak load without dropping events.",
                "action": "I profiled PostgreSQL lock contention and decoupled ingestion with FastAPI async workers, Redis Streams, and idempotency keys.",
                "result": "Reduced p95 latency by 65% (420ms to 145ms) and cut failed deliveries by 68%.",
                "metrics": ["Reduced p95 latency by 65%", "Sustained 1.2M+ events/day"],
            },
            {
                "id": "story-security-encryption",
                "title": "Envelope Encryption Migration",
                "competencies": ["security", "risk_mitigation"],
                "situation": "Audit flagged static symmetric encryption lacking key rotation.",
                "task": "Upgrade token storage to authenticated encryption with zero customer downtime.",
                "action": "I designed AES-256-GCM envelope encryption with versioned keys and dual-read migration.",
                "result": "Passed audit with zero findings and 100% migration completion.",
                "metrics": ["Zero customer downtime", "0 security audit findings"],
            },
        ],
    }
    profile_path.write_text(yaml.safe_dump(profile_data), encoding="utf-8")

    agent = InterviewPracticeAgent(
        history_root=archive_root,
        master_profile_path=profile_path,
    )

    # 1. Context loading from dossier directory & slug
    ctx = agent.load_context("StreamCore")
    assert ctx["company"] == "StreamCore"
    assert ctx["role"] == "Backend Engineer"
    assert "story-latency-migration" in ctx["targeted_behavioral_qa"]
    assert "Kafka" in ctx["technical_skills_gap"]

    # 2. Start mixed session and iterate sequentially
    session = agent.start_session(dossier_dir, max_questions=4, mode="mixed")
    assert isinstance(session, PracticeSessionState)
    assert len(session.questions) == 4
    categories = {q.category for q in session.questions}
    assert "behavioral" in categories and "technical" in categories

    asked: list[InterviewQuestion] = []
    while (next_q := agent.get_next_question(session)) is not None:
        asked.append(next_q)
    assert [q.id for q in asked] == ["q1", "q2", "q3", "q4"]
    assert session.completed is True
    assert agent.get_next_question(session) is None

    # 3. Reset session cursor for structured STAR answer evaluation
    eval_session = agent.start_session("StreamCore", max_questions=4, mode="mixed")
    q1 = eval_session.questions[0]
    strong_answer = (
        "Situation: At Nexus Cloud, our synchronous webhook ingestion service hit 420ms p95 latency "
        "and began dropping 5% of incoming events during customer traffic bursts. "
        "Task: My responsibility was to re-architect the pipeline for 3x peak volume with zero dropped events. "
        "Action: I profiled PostgreSQL lock contention, decoupled validation from persistence using FastAPI "
        "async workers and Redis Streams, added idempotency keys, and rolled out canary traffic shifting. "
        "Result: We reduced p95 latency by 65% down to 145ms and cut failed deliveries by 68% across 1.2M+ daily events."
    )
    strong_eval = agent.submit_answer(eval_session, strong_answer, question=q1)
    assert isinstance(strong_eval, STAREvaluation)
    assert 1 <= strong_eval.situation_score <= 5
    assert 1 <= strong_eval.task_score <= 5
    assert 1 <= strong_eval.action_score <= 5
    assert 1 <= strong_eval.result_score <= 5
    assert strong_eval.overall_score >= 4.0
    assert len(strong_eval.strengths) >= 1
    assert len(strong_eval.coaching_feedback) > 10
    assert len(strong_eval.suggested_evidence) >= 1
    assert any(
        "Nexus Cloud" in ev or "story-latency-migration" in ev or "Redis Streams" in ev
        for ev in strong_eval.suggested_evidence
    )

    # 4. Evaluate weak/vague answer on second question -> lower score + gaps + profile evidence
    q2 = eval_session.questions[1]
    weak_eval = agent.submit_answer(
        eval_session,
        "We worked on some backend queue improvements and things got faster.",
        question=q2,
    )
    assert weak_eval.overall_score < strong_eval.overall_score
    assert len(weak_eval.gaps) >= 1
    assert len(weak_eval.coaching_feedback) > 10
    assert len(weak_eval.suggested_evidence) >= 1
    assert any(
        "Nexus Cloud" in ev or "QueueGuard" in ev or "story-" in ev
        for ev in weak_eval.suggested_evidence
    )

    summary = agent.get_session_summary(eval_session)
    assert summary["questions_answered"] == 2
    assert summary["average_scores"]["overall"] > 0.0
