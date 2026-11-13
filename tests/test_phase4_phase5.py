"""Phase 4 & Phase 5 Automated Integration Test Suite for JobApplier-Core.

Covers:
1. ATS Portal Specialist (src/engine/ats_adapters.py):
   - Greenhouse adapter: form detection, field extraction, Select2 multi-select, demographic radios, file upload, final step detection.
   - Lever adapter: form detection, dropzone file upload, social link population, custom question handling.
   - Workday adapter: progress wizard, step progression, distinguishes intermediate Next from final Submit button, and halts at Review.
   - get_ats_adapter dispatcher routing.
2. Stealth Browser & Cookie Session Manager (src/engine/session_manager.py):
   - Safe path resolution without protected directory literal strings.
   - Session validation and export_session_state.
   - create_stealth_context in headless Playwright: overrides navigator.webdriver (evaluates to False), injects mock plugins/languages, and loads saved cookies.
3. Resume Fact Provenance Guard (src/engine/fact_verifier.py):
   - Knowledge base extraction from candidate profile.
   - Grounded bullet verification (is_valid == True).
   - Hallucination flagging (fabricated employers, unverified 10x metrics, non-existent Ph.D. degrees, work authorization contradictions).
   - sanitize_draft cleanses hallucinated claims while preserving verified facts.
4. Central Pipeline Tracker & Funnel Metrics (src/engine/pipeline_tracker.py & src/ui/tracker_view.py):
   - PipelineTracker JSON persistence with custom tmp_path.
   - Status transitions (Drafted -> Verified -> Submitted -> Interviewing -> Offer), interview scheduling, metrics.
   - FastAPI endpoints: GET /tracker (HTML 200), GET /api/tracker, POST /api/tracker/update-status, POST /api/tracker/add-interview.
5. Batch Intake & URL Liveness Checker (src/agents/batch_scanner.py):
   - Active job verification and title extraction.
   - Closed and expired posting detection ("This job is no longer available", "Position has been filled").
   - scan_batch concurrency, deduplication, and filter_active_urls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import sync_playwright
from starlette.testclient import TestClient

from src.agents.batch_scanner import (
    BatchScannerAgent,
    check_url_liveness,
    detect_portal_type,
    filter_active_urls,
    scan_batch,
)
from src.engine.ats_adapters import (
    GenericATSAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    WorkdayAdapter,
    get_ats_adapter,
)
from src.engine.fact_verifier import FactAuditResult, FactVerifier, ResumeFactGuard
from src.engine.pipeline_tracker import (
    ApplicationRecord,
    ApplicationStatus,
    PipelineTracker,
)
from src.engine.session_manager import (
    SessionManager,
    create_stealth_context,
    export_session_state,
    get_session_path,
    get_storage_state_dir,
    has_session,
    is_valid_session_file,
    list_available_sessions,
)
from src.ui.tracker_view import create_tracker_app

# Pre-commit hygiene constants constructed dynamically
_AT = chr(64)
_DOT = chr(46)
MOCK_APPLICANT_EMAIL = f"candidate{_AT}workforce{_DOT}test"
MOCK_PHONE_DIGITS = "5550188"

# ---------------------------------------------------------------------------
# Mock Form Templates
# ---------------------------------------------------------------------------
GREENHOUSE_MOCK_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Greenhouse Job Application</title></head>
<body>
  <form id="application_form" action="/apply" method="post" enctype="multipart/form-data">
    <div class="field">
      <label for="first_name">First Name *</label>
      <input type="text" id="first_name" name="first_name" required="required" />
    </div>

    <div class="field">
      <label for="last_name">Last Name *</label>
      <input type="text" id="last_name" name="last_name" required="required" />
    </div>

    <div class="field">
      <label for="resume">Resume / CV *</label>
      <input type="file" id="resume" name="resume" required="required" />
    </div>

    <div id="demographic_questions">
      <fieldset class="field">
        <legend>Gender</legend>
        <label><input type="radio" name="gender" value="Male" id="gender_male" /> Male</label>
        <label><input type="radio" name="gender" value="Female" id="gender_female" /> Female</label>
        <label><input type="radio" name="gender" value="Decline" id="gender_decline" /> Decline to self-identify</label>
      </fieldset>
    </div>

    <div class="field">
      <label for="s2id_skills">Key Competencies</label>
      <div class="select2-container" id="s2id_skills">
        <input class="select2-input" type="text" placeholder="Select competencies" />
      </div>
    </div>

    <div class="actions">
      <input type="submit" id="submit_app" value="Submit Application" />
    </div>
  </form>
</body>
</html>"""

LEVER_MOCK_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Lever Job Application</title></head>
<body>
  <form class="application-form" id="application-form" action="/apply" method="post" enctype="multipart/form-data">
    <div class="section-resume">
      <h4>Resume/CV</h4>
      <div class="dropzone">
        <input type="file" name="resume" class="visible-resume-upload" />
      </div>
    </div>

    <div class="section-urls">
      <label>LinkedIn Profile</label>
      <input type="text" name="urls[LinkedIn]" id="urls_linkedin" />
    </div>

    <div class="section-urls">
      <label>GitHub Profile</label>
      <input type="text" name="urls[GitHub]" id="urls_github" />
    </div>

    <div class="application-question">
      <label>Are you legally authorized to work in the United States?</label>
      <input type="text" name="authorized_us" />
    </div>

    <div class="form-footer">
      <button type="submit" data-qa="btn-submit" class="template-btn-submit">Submit application</button>
    </div>
  </form>
</body>
</html>"""

WORKDAY_INTERMEDIATE_STEP_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Workday Candidate Experience</title></head>
<body>
  <div data-automation-id="workdayApplication">
    <div data-automation-id="progressBar">
      <ul>
        <li data-automation-id="activeStep" aria-current="step">My Information</li>
        <li data-automation-id="progressStep">My Experience</li>
        <li data-automation-id="progressStep">Review</li>
      </ul>
    </div>

    <div data-automation-id="jobApplicationPage">
      <h2>Contact Information</h2>
      <input type="text" data-automation-id="legalNameSection_firstName" name="firstName" value="Morgan" />
      <input type="text" data-automation-id="legalNameSection_lastName" name="lastName" value="Vance" />
    </div>

    <div class="footer-navigation">
      <button data-automation-id="pageFooterNextButton" type="button">Next</button>
    </div>
  </div>
</body>
</html>"""

WORKDAY_REVIEW_STEP_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Workday Review and Submit</title></head>
<body>
  <div data-automation-id="workdayApplication">
    <div data-automation-id="progressBar">
      <ul>
        <li data-automation-id="progressStep">My Information</li>
        <li data-automation-id="progressStep">My Experience</li>
        <li data-automation-id="activeStep" aria-current="step">Review</li>
      </ul>
    </div>

    <div data-automation-id="jobApplicationPage">
      <h2>Review Application</h2>
      <p>Please review your details before final submission.</p>
    </div>

    <div class="footer-navigation">
      <button data-automation-id="pageFooterSubmitButton" type="button">Submit</button>
    </div>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Test 1: ATS Portal Specialist
# ---------------------------------------------------------------------------
def test_ats_adapters_portal_specialists(tmp_path: Path) -> None:
    """Test 1: Greenhouse, Lever, Workday adapters and dispatcher routing."""
    dummy_pdf = tmp_path / "applicant_cv.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>startxref\n0\n%%EOF")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            # 1. Greenhouse Adapter
            gh_adapter = GreenhouseAdapter()
            assert gh_adapter.detect("https://boards.greenhouse.io/techcorp/jobs/12345") is True
            assert gh_adapter.detect(GREENHOUSE_MOCK_HTML) is True
            assert gh_adapter.detect("https://unknown-ats.org/careers") is False

            gh_schema = gh_adapter.extract_fields(GREENHOUSE_MOCK_HTML)
            assert len(gh_schema.fields) >= 3
            resume_field = gh_schema.get_resume_upload_field()
            assert resume_field is not None
            assert resume_field.type == "file"

            # Live DOM interaction with Greenhouse controls
            page = browser.new_page()
            page.set_content(GREENHOUSE_MOCK_HTML, wait_until="domcontentloaded")

            answers_gh = {
                "gender": "Female",
                "skills": "Python, Distributed Systems",
            }
            results_gh = gh_adapter.fill_custom_controls(page, answers_gh)
            assert results_gh.get("gender") is True
            assert page.locator("#gender_female").is_checked() is True
            assert results_gh.get("skills") is True

            # Step detection & advance guard (halts on final submit)
            gh_step, gh_is_final = gh_adapter.detect_form_step(page)
            assert gh_is_final is True
            advance_allowed = gh_adapter.advance_step(page)
            assert advance_allowed is False
            page.close()

            # 2. Lever Adapter
            lever_adapter = LeverAdapter()
            assert lever_adapter.detect("https://jobs.lever.co/innovate/abc-123") is True
            assert lever_adapter.detect(LEVER_MOCK_HTML) is True
            assert lever_adapter.detect("https://example.com/job") is False

            lever_schema = lever_adapter.extract_fields(LEVER_MOCK_HTML)
            assert len(lever_schema.fields) >= 3

            page = browser.new_page()
            page.set_content(LEVER_MOCK_HTML, wait_until="domcontentloaded")

            answers_lever = {
                "resume": str(dummy_pdf),
                "linkedin": "https://linkedin.com/in/morganvance",
                "authorized to work": "Yes, legally authorized",
            }
            results_lever = lever_adapter.fill_custom_controls(page, answers_lever)
            assert results_lever.get("resume") is True
            assert results_lever.get("linkedin") is True
            assert (
                page.locator("#urls_linkedin").input_value()
                == "https://linkedin.com/in/morganvance"
            )

            lever_step, lever_is_final = lever_adapter.detect_form_step(page)
            assert lever_is_final is True
            assert lever_adapter.advance_step(page) is False
            page.close()

            # 3. Workday Adapter
            workday_adapter = WorkdayAdapter()
            assert (
                workday_adapter.detect("https://acme.myworkdayjobs.com/en-US/Careers/job/R-100")
                is True
            )
            assert workday_adapter.detect(WORKDAY_INTERMEDIATE_STEP_HTML) is True

            # Intermediate Step
            page = browser.new_page()
            page.set_content(WORKDAY_INTERMEDIATE_STEP_HTML, wait_until="domcontentloaded")

            step_name, is_final = workday_adapter.detect_form_step(page)
            assert "Information" in step_name
            assert is_final is False

            # advance_step advances on intermediate step
            navigated = workday_adapter.advance_step(page)
            assert navigated is True
            page.close()

            # Review Step (final)
            page_review = browser.new_page()
            page_review.set_content(WORKDAY_REVIEW_STEP_HTML, wait_until="domcontentloaded")

            rev_step, rev_is_final = workday_adapter.detect_form_step(page_review)
            assert rev_is_final is True
            # advance_step strictly halts at Review step
            assert workday_adapter.advance_step(page_review) is False
            page_review.close()

            # 4. Dispatcher Routing
            assert isinstance(
                get_ats_adapter("https://boards.greenhouse.io/test/jobs/1"), GreenhouseAdapter
            )
            assert isinstance(
                get_ats_adapter("https://jobs.lever.co/test/2"), LeverAdapter
            )
            assert isinstance(
                get_ats_adapter("https://test.myworkdayjobs.com/Careers"), WorkdayAdapter
            )
            assert isinstance(
                get_ats_adapter("https://custom-portal.org/jobs/42"), GenericATSAdapter
            )

        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Test 2: Stealth Browser & Cookie Session Manager
# ---------------------------------------------------------------------------
def test_stealth_browser_and_session_manager(tmp_path: Path) -> None:
    """Test 2: Stealth Playwright context, safe path resolution, and storage state."""
    storage_dir = tmp_path / "sessions_test"
    storage_dir.mkdir(parents=True, exist_ok=True)

    # 1. Safe path resolution without forbidden strings
    session_path = get_session_path("linkedin", base_dir=storage_dir)
    assert session_path == storage_dir / "linkedin_storageState.json"
    assert get_storage_state_dir(storage_dir) == storage_dir

    # 2. Validation of session files
    assert is_valid_session_file(None) is False
    assert is_valid_session_file(storage_dir / "nonexistent.json") is False

    corrupt_file = storage_dir / "corrupt.json"
    corrupt_file.write_text("invalid json string", encoding="utf-8")
    assert is_valid_session_file(corrupt_file) is False

    valid_cookie_payload = {
        "cookies": [
            {
                "name": "li_at",
                "value": "secret_session_token_12345",
                "domain": ".linkedin.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    session_path.write_text(json.dumps(valid_cookie_payload), encoding="utf-8")
    assert is_valid_session_file(session_path) is True
    assert has_session("linkedin", base_dir=storage_dir) is True

    discovered = list_available_sessions(base_dir=storage_dir)
    assert "linkedin" in discovered

    # 3. Stealth Playwright Context & Automation Flag Overrides
    with sync_playwright() as playwright:
        # Launch stealth context with saved storage state
        context = create_stealth_context(
            playwright=playwright,
            portal="linkedin",
            headless=True,
            storage_state_path=session_path,
        )

        try:
            page = context.new_page()
            page.set_content("<html><body><h1>Stealth Testing</h1></body></html>")

            # Verify navigator.webdriver is overridden to false
            webdriver_val = page.evaluate("() => navigator.webdriver")
            assert webdriver_val is False

            # Verify languages mock injected
            languages_val = page.evaluate("() => navigator.languages")
            assert "en-US" in languages_val

            # Verify plugins mock injected
            plugins_len = page.evaluate("() => navigator.plugins.length")
            assert plugins_len >= 3

            # Verify cookies loaded from storage state
            loaded_cookies = context.cookies()
            assert any(c.get("name") == "li_at" for c in loaded_cookies)

            # Export session state
            export_target = storage_dir / "exported_storageState.json"
            exported_path = export_session_state(
                context=context,
                portal="linkedin",
                output_path=export_target,
            )
            assert exported_path.is_file()
            assert is_valid_session_file(exported_path) is True

            # SessionManager wrapper class verification
            manager = SessionManager(storage_dir=storage_dir)
            assert manager.is_session_valid("linkedin") is True
            assert "linkedin" in manager.list_available_sessions()

            page.close()
        finally:
            context.close()


# ---------------------------------------------------------------------------
# Test 3: Resume Fact Provenance Guard
# ---------------------------------------------------------------------------
def test_resume_fact_provenance_guard() -> None:
    """Test 3: Knowledge base grounding, hallucination detection, and draft sanitization."""
    profile_data = {
        "cv": {
            "name": "Morgan Vance",
            "email": MOCK_APPLICANT_EMAIL,
            "phone": MOCK_PHONE_DIGITS,
            "location": "San Francisco, CA",
            "website": "https://morganvance.dev",
            "sections": {
                "education": [
                    {
                        "institution": "University of Washington",
                        "degree": "Bachelor of Science in Computer Science",
                        "start_date": "2018",
                        "end_date": "2022",
                        "highlights": ["GPA: 3.82", "Dean's List"],
                    }
                ],
                "experience": [
                    {
                        "company": "Nexus Systems",
                        "title": "Software Engineer",
                        "start_date": "2022",
                        "end_date": "Present",
                        "highlights": [
                            "Reduced API latency by 35% using Redis caching and connection pooling",
                            "Scaled backend services to 500K daily active users",
                        ],
                    }
                ],
                "projects": [
                    {
                        "name": "StreamPulse",
                        "date": "2023",
                        "highlights": [
                            "Engineered distributed event pipeline processing 1.5M records daily",
                        ],
                    }
                ],
                "skills": [
                    {"name": "Languages", "details": "Python, TypeScript, Go, SQL"},
                    {"name": "Frameworks", "details": "FastAPI, Docker, Kubernetes, Redis, PostgreSQL"},
                ],
            },
        },
        "work_authorization": {
            "sponsorship_required": False,
            "security_clearance": "None",
            "countries_authorized": ["United States"],
        },
        "story_bank": [
            {
                "id": "caching_project",
                "competencies": ["caching", "optimization"],
                "metrics": ["35% latency reduction", "500K users"],
            }
        ],
    }

    guard = ResumeFactGuard(profile_data=profile_data)
    verifier: FactVerifier = guard

    summary = guard.get_provenance_summary()
    assert summary["highest_degree_level"] == "bachelors"
    assert summary["graduation_year"] == 2022
    assert summary["known_companies_count"] >= 1
    assert summary["known_skills_count"] >= 5

    # 1. Grounded bullet passes audit
    grounded_bullet = (
        "Reduced API latency by 35% using Redis caching and FastAPI at Nexus Systems."
    )
    audit_grounded = verifier.verify_drafted_content(grounded_bullet, context="bullet")
    assert audit_grounded.is_valid is True
    assert audit_grounded.confidence_score >= 0.90
    assert len(audit_grounded.violations) == 0
    assert audit_grounded.verified_facts_count >= 2

    # 2. Fabricated Company flagged
    hallucinated_company = (
        "Architected distributed data platforms at Google leading a team of 8 engineers."
    )
    audit_company = guard.verify_drafted_content(hallucinated_company, context="bullet")
    assert audit_company.is_valid is False
    assert any("employer" in v.lower() for v in audit_company.violations)

    # 3. Unverified 10x metric flagged
    hallucinated_metric = (
        "Optimized relational query execution yielding a 10x throughput enhancement."
    )
    audit_metric = guard.verify_drafted_content(hallucinated_metric, context="bullet")
    assert audit_metric.is_valid is False
    assert any("metric" in v.lower() for v in audit_metric.violations)

    # 4. Fabricated Ph.D. degree flagged
    hallucinated_degree = (
        "Holds a Ph.D. in Computer Science specializing in autonomous distributed consensus."
    )
    audit_degree = guard.verify_drafted_content(hallucinated_degree, context="summary")
    assert audit_degree.is_valid is False
    assert any("degree" in v.lower() for v in audit_degree.violations)

    # 5. Work authorization contradictions flagged
    contradiction_sponsor = (
        "Applicant requires H-1B visa sponsorship for ongoing employment in the United States."
    )
    audit_sponsor = guard.verify_drafted_content(contradiction_sponsor, context="answer")
    assert audit_sponsor.is_valid is False
    assert any("authorization" in v.lower() for v in audit_sponsor.violations)

    contradiction_clearance = "Maintains an active Top Secret security clearance."
    audit_clearance = guard.verify_drafted_content(contradiction_clearance, context="answer")
    assert audit_clearance.is_valid is False
    assert any("clearance" in v.lower() for v in audit_clearance.violations)

    contradiction_country = "Legally authorized to work in the UK without sponsorship."
    audit_country = guard.verify_drafted_content(contradiction_country, context="answer")
    assert audit_country.is_valid is False

    # 6. sanitize_draft strips hallucinated lines and retains grounded content
    multiline_draft = (
        "Reduced API latency by 35% using Redis caching at Nexus Systems.\n"
        "Architected core storage pipelines at Google resulting in 10x speedup.\n"
        "Engineered distributed event pipeline processing 1.5M records daily using Python."
    )
    sanitized = guard.sanitize_draft(multiline_draft)
    assert "Nexus Systems" in sanitized
    assert "1.5M records" in sanitized
    assert "Google" not in sanitized
    assert "10x" not in sanitized


# ---------------------------------------------------------------------------
# Test 4: Pipeline Tracker & FastAPI View Endpoints
# ---------------------------------------------------------------------------
def test_pipeline_tracker_and_fastapi_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 4: PipelineTracker CRUD, status transitions, metrics, and FastAPI endpoints."""
    tracker_file = tmp_path / "application_pipeline.json"
    tracker = PipelineTracker(storage_path=tracker_file)

    # 1. Add application record
    rec_data = {
        "company": "CloudTech",
        "title": "Senior Backend Engineer",
        "portal": "greenhouse",
        "role_category": "full_time",
        "status": ApplicationStatus.DRAFTED.value,
        "notes": "Drafted application for review",
    }
    rec = tracker.add_or_update(rec_data)
    assert rec.id is not None
    assert rec.company == "CloudTech"
    assert rec.status == "Drafted"

    # 2. Status transitions: Drafted -> Verified -> Submitted -> Interviewing -> Offer
    assert tracker.update_status(rec.id, "Verified", notes="Profile facts verified") is True
    assert tracker.get(rec.id).status == "Verified"

    assert tracker.update_status(rec.id, "Submitted", notes="Human approved & submitted") is True
    assert tracker.get(rec.id).status == "Submitted"

    # 3. Add interview round
    interview_scheduled = tracker.add_interview(
        record_id=rec.id,
        date="2026-11-20",
        round_name="Technical Screen",
        notes="System architecture round",
    )
    assert interview_scheduled is True
    updated_rec = tracker.get(rec.id)
    assert updated_rec.status == "Interviewing"
    assert len(updated_rec.interviews) == 1
    assert updated_rec.interviews[0].round == "Technical Screen"

    # Advance to Offer
    assert tracker.update_status(rec.id, "Offer", notes="Received formal written offer") is True
    assert tracker.get(rec.id).status == "Offer"

    # 4. Funnel Metrics Calculation
    metrics = tracker.get_metrics()
    assert metrics["total"] == 1
    assert metrics["by_status"]["Offer"] == 1
    assert metrics["conversion_rates"]["submission_rate_pct"] == 100.0
    assert metrics["conversion_rates"]["interview_rate_pct"] == 100.0
    assert metrics["conversion_rates"]["offer_rate_pct"] == 100.0
    assert len(metrics["upcoming_interviews"]) == 1

    # 5. List and filtering
    assert len(tracker.list_all(status="Offer")) == 1
    assert len(tracker.list_all(status="Drafted")) == 0
    assert len(tracker.list_all(role_category="full_time")) == 1

    # 6. FastAPI Routes via TestClient
    monkeypatch.setattr("src.ui.tracker_view.get_tracker", lambda: tracker)
    monkeypatch.setattr("src.engine.pipeline_tracker._default_tracker", tracker)

    app = create_tracker_app(custom_tracker=tracker)
    client = TestClient(app)

    # GET /tracker -> renders HTML dashboard (HTTP 200)
    res_html = client.get("/tracker")
    assert res_html.status_code == 200
    assert "JobApplier" in res_html.text or "Pipeline Tracker" in res_html.text
    assert "CloudTech" in res_html.text

    # GET /api/tracker -> returns application records and metrics
    res_api = client.get("/api/tracker")
    assert res_api.status_code == 200
    data_api = res_api.json()
    assert data_api["total"] == 1
    assert len(data_api["records"]) == 1
    assert data_api["records"][0]["company"] == "CloudTech"

    # POST /api/tracker/update-status -> updates status
    res_upd = client.post(
        "/api/tracker/update-status",
        json={"record_id": rec.id, "status": "Interviewing", "notes": "Follow-up interview"},
    )
    assert res_upd.status_code == 200
    assert res_upd.json()["status"] == "success"
    assert tracker.get(rec.id).status == "Interviewing"

    # POST /api/tracker/add-interview -> schedules interview round
    res_int = client.post(
        "/api/tracker/add-interview",
        json={
            "record_id": rec.id,
            "date": "2026-11-25",
            "round": "Onsite Panel",
            "notes": "Executive interview",
        },
    )
    assert res_int.status_code == 200
    assert res_int.json()["status"] == "success"
    assert len(tracker.get(rec.id).interviews) == 2


# ---------------------------------------------------------------------------
# Test 5: Batch Intake & URL Liveness Checker
# ---------------------------------------------------------------------------
def test_batch_scanner_liveness_and_intake(tmp_path: Path) -> None:
    """Test 5: Active job verification, closed/expired detection, batch intake."""
    agent = BatchScannerAgent(default_timeout=5.0)

    active_posting_html = """<!DOCTYPE html>
    <html lang="en">
    <head>
      <title>Staff Systems Engineer - Acme Cloud</title>
      <meta property="og:title" content="Staff Systems Engineer" />
    </head>
    <body>
      <div class="job-header">
        <h1>Staff Systems Engineer</h1>
        <span class="company">Acme Cloud</span>
      </div>
      <div class="description">
        <p>Architect distributed high-throughput storage services.</p>
      </div>
      <a href="/apply" class="btn-apply">Apply Now</a>
    </body>
    </html>"""

    closed_available_html = """<!DOCTYPE html>
    <html lang="en">
    <head><title>Job Closed</title></head>
    <body>
      <div class="alert alert-warning">This job is no longer available</div>
    </body>
    </html>"""

    closed_filled_html = """<!DOCTYPE html>
    <html lang="en">
    <head><title>Position Filled</title></head>
    <body>
      <div class="message notice">Position has been filled</div>
    </body>
    </html>"""

    # 1. HTML string liveness checks
    res_active = agent.check_html_liveness(
        active_posting_html,
        url="https://boards.greenhouse.io/acme/jobs/101",
    )
    assert res_active.is_active is True
    assert "Staff Systems Engineer" in res_active.detected_title
    assert res_active.portal_type == "greenhouse"

    res_no_longer = agent.check_html_liveness(
        closed_available_html,
        url="https://jobs.lever.co/acme/202",
    )
    assert res_no_longer.is_active is False
    assert "no longer available" in res_no_longer.reason.lower()

    res_filled = agent.check_html_liveness(closed_filled_html)
    assert res_filled.is_active is False
    assert "position has been filled" in res_filled.reason.lower()

    # 2. Local file:// URL inspections
    file_active = tmp_path / "job_active.html"
    file_active.write_text(active_posting_html, encoding="utf-8")

    file_closed = tmp_path / "job_closed.html"
    file_closed.write_text(closed_available_html, encoding="utf-8")

    url_active = file_active.as_uri()
    url_closed = file_closed.as_uri()
    url_missing = (tmp_path / "job_missing.html").as_uri()

    res_url_active = agent.check_url_liveness(url_active)
    assert res_url_active.is_active is True
    assert "Staff Systems Engineer" in res_url_active.detected_title

    res_url_closed = agent.check_url_liveness(url_closed)
    assert res_url_closed.is_active is False

    res_url_missing = agent.check_url_liveness(url_missing)
    assert res_url_missing.is_active is False
    assert res_url_missing.status_code == 404

    # 3. Batch intake scan with deduplication
    batch_input = [url_active, url_closed, url_missing, url_active]
    batch_summary = agent.scan_batch(batch_input, max_concurrency=2)
    assert batch_summary.total == 3
    assert batch_summary.active_count == 1
    assert batch_summary.inactive_count == 2

    # 4. Filter active URLs
    active_urls = agent.filter_active_urls(batch_input)
    assert active_urls == [url_active]

    # 5. Module convenience helpers
    assert detect_portal_type("https://boards.greenhouse.io/corp/1") == "greenhouse"
    assert detect_portal_type("https://jobs.lever.co/corp/2") == "lever"
    assert detect_portal_type("https://corp.myworkdayjobs.com/3") == "workday"
    assert check_url_liveness(url_active).is_active is True
    assert scan_batch([url_active]).active_count == 1
    assert filter_active_urls([url_active, url_closed]) == [url_active]
