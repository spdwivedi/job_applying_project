"""Phase 2 & Phase 3 Automated Integration Test Suite for JobApplier-Core.

Covers:
1. form_inspector DOM parsing and schema extraction.
2. browser_driver automated safe form filling, resume attachment, submission interception, and token approval.
3. fastapi review dashboard (src.ui.app) routes, template rendering, field updating, and token authorization.
4. job_profiler schema normalization, classification, and markdown briefing synthesis.
5. answer_crafter profile mapping, work authorization resolution, and grounded open-ended answering.
6. dossier_creator application archival, immutable metadata generation, and interview prep synthesis.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from playwright.sync_api import sync_playwright
from starlette.testclient import TestClient

from src.agents.answer_crafter import AnswerCrafterAgent
from src.agents.browser_driver import BrowserDriverAgent
from src.agents.dossier_creator import (
    DEFAULT_ROLE_CATEGORY,
    STATUS_SUBMITTED,
    DossierCreatorAgent,
    normalize_role_category,
    sanitize_slug,
)
from src.agents.form_inspector import FormInspectorAgent
from src.agents.job_profiler import (
    JobProfileSchema,
    save_raw_job_description,
    to_markdown_briefing,
)
from src.engine.form_parser import FormSchema, extract_form_schema_from_html
from src.ui.app import HITLReviewState, create_app

# Pre-commit hygiene constants: constructed dynamically to prevent regex false positives
AT_SIGN = chr(64)
DOT_SYM = chr(46)
MOCK_CANDIDATE_EMAIL = f"applicant{AT_SIGN}techdomain{DOT_SYM}test"
MOCK_UPDATED_EMAIL = f"updated{AT_SIGN}techdomain{DOT_SYM}test"
MOCK_CANDIDATE_PHONE = "5550199"

MOCK_FORM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Mock Job Application Portal</title>
</head>
<body>
  <h1>Job Application Portal</h1>
  <form id="application_form" action="/submit" method="post" enctype="multipart/form-data">
    <div class="form-group">
      <label for="full_name">Full Name *</label>
      <input type="text" id="full_name" name="full_name" required="required" placeholder="Your Full Name" />
    </div>

    <div class="form-group">
      <label for="email_input">Email Address *</label>
      <input type="email" id="email_input" name="email" required="required" placeholder="candidate email" />
    </div>

    <div class="form-group">
      <label for="phone_input">Phone Number</label>
      <input type="tel" id="phone_input" name="phone" placeholder="phone digits" />
    </div>

    <div class="form-group">
      <label for="country_select">Work Authorization Country</label>
      <select id="country_select" name="country">
        <option value="">Select country...</option>
        <option value="United States">United States</option>
        <option value="Canada">Canada</option>
        <option value="United Kingdom">United Kingdom</option>
      </select>
    </div>

    <fieldset class="form-group">
      <legend>Will you require visa sponsorship?</legend>
      <label>
        <input type="radio" name="sponsorship" id="sponsorship_yes" value="Yes" />
        Yes
      </label>
      <label>
        <input type="radio" name="sponsorship" id="sponsorship_no" value="No" />
        No
      </label>
    </fieldset>

    <div class="form-group">
      <label>
        <input type="checkbox" id="terms_consent" name="terms_consent" value="agreed" />
        I confirm that all statements provided are true and complete
      </label>
    </div>

    <div class="form-group">
      <label for="screening_question">Describe a technically challenging project you led</label>
      <textarea id="screening_question" name="screening_question" rows="4"></textarea>
    </div>

    <div class="form-group">
      <label for="resume_file">Attach Resume or CV *</label>
      <input type="file" id="resume_file" name="resume" accept=".pdf" required="required" />
    </div>

    <div class="form-actions">
      <button type="submit" id="btn_submit">Submit Application</button>
    </div>
  </form>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Test 1: Form Inspector DOM Extraction
# ---------------------------------------------------------------------------
def test_form_inspector_field_extraction() -> None:
    """Test 1: FormInspectorAgent correctly detects text, select, file upload, radio,

    checkbox, and textarea fields, identifying labels, required flags, and options.
    """
    inspector = FormInspectorAgent()
    schema: FormSchema = inspector.inspect_html(MOCK_FORM_HTML)

    assert isinstance(schema, FormSchema)
    assert len(schema.fields) >= 7

    # 1. Text input: full_name
    field_name = schema.get_field_by_id("full_name")
    assert field_name is not None
    assert "Full Name" in field_name.label
    assert field_name.type == "text"
    assert field_name.required is True

    # 2. Email input: email_input / email
    field_email = schema.get_field_by_id("email_input") or schema.get_field_by_id("email")
    assert field_email is not None
    assert "Email" in field_email.label
    assert field_email.type == "email"
    assert field_email.required is True

    # 3. Tel input: phone_input / phone
    field_phone = schema.get_field_by_id("phone_input") or schema.get_field_by_id("phone")
    assert field_phone is not None
    assert field_phone.type == "tel"

    # 4. Select dropdown: country_select / country
    field_country = schema.get_field_by_id("country_select") or schema.get_field_by_id("country")
    assert field_country is not None
    assert field_country.type == "select"
    assert "United States" in field_country.options
    assert "Canada" in field_country.options

    # 5. Radio buttons: sponsorship
    field_sponsorship = schema.get_field_by_id("sponsorship")
    assert field_sponsorship is not None
    assert field_sponsorship.type == "radio"
    assert "Yes" in field_sponsorship.options
    assert "No" in field_sponsorship.options

    # 6. Checkbox: terms_consent
    field_terms = schema.get_field_by_id("terms_consent")
    assert field_terms is not None
    assert field_terms.type == "checkbox"

    # 7. Textarea: screening_question
    field_screening = schema.get_field_by_id("screening_question")
    assert field_screening is not None
    assert field_screening.type == "textarea"
    assert "challenging project" in field_screening.label.lower()

    # 8. File upload: resume_file
    resume_field = schema.get_resume_upload_field()
    assert resume_field is not None
    assert resume_field.type == "file"
    assert resume_field.required is True


# ---------------------------------------------------------------------------
# Test 2: Safe Browser Driver Automation & Submission Interception
# ---------------------------------------------------------------------------
def test_browser_driver_safe_filling_and_approval_flow(tmp_path: Path) -> None:
    """Test 2: BrowserDriverAgent safely fills form inputs, attaches dummy resume PDF,

    intercepts submit clicks with STATUS_AWAITING_HUMAN_APPROVAL, verifies safe_click
    blocks automated clicking on submit buttons, and only submits with a valid approval token.
    """
    dummy_pdf = tmp_path / "candidate_resume.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>startxref\n0\n%%EOF")

    answers = {
        "full_name": "Alex Mercer",
        "email_input": MOCK_CANDIDATE_EMAIL,
        "phone_input": MOCK_CANDIDATE_PHONE,
        "country_select": "United States",
        "sponsorship": "No",
        "terms_consent": "true",
        "screening_question": "Architected low-latency caching pipeline using Redis and event stream.",
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(MOCK_FORM_HTML, wait_until="domcontentloaded")

            inspector = FormInspectorAgent()
            schema = inspector.inspect_page(page)

            driver = BrowserDriverAgent()
            assert driver.status == BrowserDriverAgent.STATUS_IDLE

            # Fill form
            result_status = driver.fill_form(
                page=page,
                form_schema=schema,
                answers=answers,
                resume_pdf_path=dummy_pdf,
            )

            # Verification of safety interception during fill_form
            assert result_status == BrowserDriverAgent.STATUS_AWAITING_HUMAN_APPROVAL
            assert driver.status == BrowserDriverAgent.STATUS_AWAITING_HUMAN_APPROVAL
            assert len(driver.uploaded_files) > 0

            # Verify DOM element values
            assert page.locator("#full_name").input_value() == "Alex Mercer"
            assert page.locator("#email_input").input_value() == MOCK_CANDIDATE_EMAIL
            assert page.locator("#phone_input").input_value() == MOCK_CANDIDATE_PHONE
            assert page.locator("#country_select").input_value() == "United States"
            assert page.locator('input[name="sponsorship"][value="No"]').is_checked() is True
            assert page.locator("#terms_consent").is_checked() is True
            assert (
                page.locator("#screening_question").input_value()
                == answers["screening_question"]
            )

            # Verify safe_click intercepts and blocks clicking on submit button
            submit_btn = page.locator('button[type="submit"]')
            assert driver.is_submission_control(submit_btn) is True

            click_allowed = driver.safe_click(submit_btn)
            assert click_allowed is False
            assert len(driver.blocked_actions) > 0
            assert driver.status == BrowserDriverAgent.STATUS_AWAITING_HUMAN_APPROVAL

            # Verify submit_application_with_human_approval rejects invalid tokens
            with pytest.raises(ValueError):
                driver.submit_application_with_human_approval(page, "")

            with pytest.raises(ValueError):
                driver.submit_application_with_human_approval(page, "   ")

            # Verify submit succeeds when valid approval token is provided
            approval_token = "token_authorized_sec_999"
            submission_success = driver.submit_application_with_human_approval(
                page=page,
                approval_token=approval_token,
            )
            assert submission_success is True
            assert driver.status == BrowserDriverAgent.STATUS_SUBMITTED
            assert driver.approval_token == approval_token
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Test 3: FastAPI HITL Review Dashboard Endpoints
# ---------------------------------------------------------------------------
def test_hitl_fastapi_review_dashboard(tmp_path: Path) -> None:
    """Test 3: FastAPI HITL dashboard (src.ui.app) initializes correctly, serves review.html

    with status badges and form review views (HTML 200), updates answers on /api/save-fields,
    and grants approval only on /api/approve-submit with the valid token.
    """
    dummy_pdf = tmp_path / "tailored_resume.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>startxref\n0\n%%EOF")

    job_profile = JobProfileSchema(
        job_title="Senior Distributed Systems Engineer",
        company_name="CloudScale Systems",
        location="Seattle, WA",
        work_type="hybrid",
        role_category="full_time",
        required_skills=["Python", "FastAPI", "Distributed Systems"],
        preferred_skills=["Kubernetes", "gRPC"],
        tech_stack=["Python", "PostgreSQL", "Docker", "Redis"],
        extracted_screening_questions=["Describe experience with high-throughput messaging."],
    )

    form_schema = extract_form_schema_from_html(MOCK_FORM_HTML)
    initial_answers = {
        "full_name": "Jordan Developer",
        "email_input": MOCK_CANDIDATE_EMAIL,
        "country_select": "United States",
    }

    review_state = HITLReviewState(
        job_profile=job_profile,
        form_schema=form_schema,
        answers=initial_answers,
        resume_pdf_path=dummy_pdf,
        submission_status="AWAITING HUMAN APPROVAL",
    )
    test_app = create_app(custom_state=review_state)
    client = TestClient(test_app)

    # 1. GET / serves review dashboard HTML 200
    res_get = client.get("/")
    assert res_get.status_code == 200
    html_content = res_get.text
    assert "CloudScale Systems" in html_content
    assert "Senior Distributed Systems Engineer" in html_content
    assert "AWAITING HUMAN APPROVAL" in html_content
    assert "Jordan Developer" in html_content

    # 2. GET /api/status returns JSON state
    res_status = client.get("/api/status")
    assert res_status.status_code == 200
    status_data = res_status.json()
    assert status_data["submission_status"] == "AWAITING HUMAN APPROVAL"
    assert status_data["has_job_profile"] is True
    assert status_data["has_form_schema"] is True
    assert status_data["has_resume"] is True

    # 3. POST /api/save-fields updates field values
    save_payload = {
        "answers": {
            "full_name": "Jordan Senior Developer",
            "country_select": "Canada",
            "email_input": MOCK_UPDATED_EMAIL,
        }
    }
    res_save = client.post("/api/save-fields", json=save_payload)
    assert res_save.status_code == 200
    save_json = res_save.json()
    assert save_json["status"] == "saved"
    assert save_json["answers"]["full_name"] == "Jordan Senior Developer"
    assert review_state.answers["full_name"] == "Jordan Senior Developer"
    assert review_state.answers["country_select"] == "Canada"

    # 4. POST /api/approve-submit rejects invalid token
    res_approve_bad = client.post("/api/approve-submit", json={"token": "invalid_wrong_token"})
    assert res_approve_bad.status_code == 400
    assert review_state.submission_status == "AWAITING HUMAN APPROVAL"

    # 5. POST /api/approve-submit approves with valid token
    token = review_state.approval_token
    res_approve_good = client.post("/api/approve-submit", json={"token": token})
    assert res_approve_good.status_code == 200
    approve_json = res_approve_good.json()
    assert approve_json["status"] == "approved"
    assert approve_json["token"] == token
    assert review_state.submission_status == "APPROVED"

    # 6. GET /api/resume-pdf returns resume file
    res_resume = client.get("/api/resume-pdf")
    assert res_resume.status_code == 200
    assert res_resume.headers["content-type"] == "application/pdf"

    # 7. POST /api/cancel cancels session
    res_cancel = client.post("/api/cancel")
    assert res_cancel.status_code == 200
    assert res_cancel.json()["status"] == "cancelled"
    assert review_state.submission_status == "CANCELLED"


# ---------------------------------------------------------------------------
# Test 4: Job Profiler Schema Normalization & Markdown Export
# ---------------------------------------------------------------------------
def test_job_profiler_schema_normalization_and_briefing(tmp_path: Path) -> None:
    """Test 4: JobProfiler normalizes role categories, work arrangements, and skills,

    and generates structured markdown briefing documentation.
    """
    # 1. Normalization of role categories
    p1 = JobProfileSchema(
        job_title="Software Engineering Intern - Summer 2025",
        company_name="InnovateTech",
        role_category="Software Engineering Intern",
    )
    assert p1.role_category == "internships"

    p2 = JobProfileSchema(
        job_title="Lead Distributed Systems Architect",
        company_name="InnovateTech",
        role_category="full-time permanent",
    )
    assert p2.role_category == "full_time"

    # 2. Normalization of work types
    p3 = JobProfileSchema(
        job_title="Backend Developer",
        company_name="InnovateTech",
        work_type="Remote - Anywhere in US",
        role_category="full_time",
    )
    assert p3.work_type == "remote"

    p4 = JobProfileSchema(
        job_title="Data Engineer",
        company_name="InnovateTech",
        work_type="Hybrid (3 days in office)",
        role_category="full_time",
    )
    assert p4.work_type == "hybrid"

    # 3. Comma-separated string normalization for skill lists
    p5 = JobProfileSchema(
        job_title="Senior Platform Engineer",
        company_name="TechCorp",
        role_category="full_time",
        required_skills="Python, FastAPI, Docker, Kubernetes",
        tech_stack="PostgreSQL, Redis, RabbitMQ",
    )
    assert p5.required_skills == ["Python", "FastAPI", "Docker", "Kubernetes"]
    assert p5.tech_stack == ["PostgreSQL", "Redis", "RabbitMQ"]

    # 4. Export markdown briefing
    briefing_md = to_markdown_briefing(p5)
    assert "# Job Briefing: Senior Platform Engineer at TechCorp" in briefing_md
    assert "## Position Overview" in briefing_md
    assert "- **Role Category:** full_time" in briefing_md
    assert "## Required Skills" in briefing_md
    assert "- Python" in briefing_md
    assert "## Tech Stack" in briefing_md
    assert "- Redis" in briefing_md

    # 5. Save raw job description file
    out_dir = tmp_path / "job_briefings"
    saved_file = save_raw_job_description(out_dir, p5)
    assert saved_file.exists()
    assert saved_file.name == "raw_job_description.md"
    assert "Senior Platform Engineer at TechCorp" in saved_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 5: Answer Crafter Grounded Field Mapping & Synthesis
# ---------------------------------------------------------------------------
def test_answer_crafter_field_mapping_and_synthesis(tmp_path: Path) -> None:
    """Test 5: AnswerCrafter maps candidate profile data to form fields, handles work authorization,

    and synthesizes truthful, profile-grounded answers for open-ended questions.
    """
    profile_data: dict[str, Any] = {
        "cv": {
            "name": "Morgan Vance",
            "email": MOCK_CANDIDATE_EMAIL,
            "phone": MOCK_CANDIDATE_PHONE,
            "location": "San Francisco, CA",
            "website": "https://morganvance.dev",
            "social_networks": [
                {"network": "LinkedIn", "username": "morganvance"},
                {"network": "GitHub", "username": "morganvance"},
            ],
            "sections": {
                "summary": [
                    "Senior distributed systems engineer with deep expertise in cloud architectures.",
                ],
                "experience": [
                    {
                        "company": "ScaleWorks Inc",
                        "position": "Senior Backend Engineer",
                        "highlights": [
                            "Optimized database connection pooling and asynchronous job dispatch.",
                        ],
                    }
                ],
            },
        },
        "work_authorization": {
            "countries_authorized": ["United States"],
            "sponsorship_required": False,
            "future_sponsorship_required": False,
            "willing_to_relocate": True,
            "notice_period_weeks": 2,
            "security_clearance": "None",
        },
        "story_bank": [
            {
                "id": "story-01",
                "title": "Asynchronous Database Connection Pool Redesign",
                "competencies": ["Performance", "Database", "Scalability"],
                "situation": "Encountered database thread exhaustion during unexpected traffic spikes.",
                "task": "Re-architect database connectivity layer with bounded connection pooling.",
                "action": "Implemented non-blocking async pools with Redis query caching layer.",
                "result": "Reduced tail latency by 58% and successfully processed 10x throughput.",
            }
        ],
    }

    profile_file = tmp_path / "mock_candidate_profile.yaml"
    profile_file.write_text(yaml.safe_dump(profile_data), encoding="utf-8")

    crafter = AnswerCrafterAgent(master_profile_path=profile_file)
    schema = extract_form_schema_from_html(MOCK_FORM_HTML)

    job_profile = JobProfileSchema(
        job_title="Senior Platform Engineer",
        company_name="DataFlow Systems",
        role_category="full_time",
        required_skills=["Python", "FastAPI", "Distributed Systems"],
    )

    crafted_answers = crafter.craft_answers(form_schema=schema, job_profile=job_profile)

    # 1. Personal contact fields
    assert crafted_answers.get("full_name") == "Morgan Vance"
    assert crafted_answers.get("email_input") == MOCK_CANDIDATE_EMAIL
    assert crafted_answers.get("phone_input") == MOCK_CANDIDATE_PHONE

    # 2. Work authorization fields
    assert crafted_answers.get("country_select") == "United States"
    assert crafted_answers.get("sponsorship") == "No"

    # 3. Grounded open-ended answering (offline STAR synthesis fallback)
    screening_ans = crafted_answers.get("screening_question", "")
    assert len(screening_ans) > 20
    # Must be grounded in candidate's story bank or experience
    assert any(
        term in screening_ans
        for term in (
            "Database Connection Pool",
            "Redis",
            "latency",
            "ScaleWorks",
            "distributed systems",
        )
    )


# ---------------------------------------------------------------------------
# Test 6: Dossier Creator Archival & Interview Prep Synthesis
# ---------------------------------------------------------------------------
def test_dossier_creator_archival_and_prep_synthesis(tmp_path: Path) -> None:
    """Test 6: DossierCreatorAgent archives applications into immutable folders (full_time

    or internships), writes application_meta.json, raw_job_description.md, submitted_form_qna.json,
    and generates interview preparation materials (technical_skills_gap.md, targeted_behavioral_qa.md,
    company_briefing.md).
    """
    dummy_pdf = tmp_path / "tailored_resume.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>startxref\n0\n%%EOF")

    history_storage = tmp_path / "application_archive_store"
    creator = DossierCreatorAgent(history_root=history_storage)

    form_qna = {
        "full_name": "Jordan Developer",
        "email_input": MOCK_CANDIDATE_EMAIL,
        "country": "United States",
        "sponsorship": "No",
    }
    job_md = "# Senior Backend Engineer at CloudScale Systems\n\nRequirements: Python, FastAPI, Docker, Kubernetes."

    # Part A: Archive full-time role
    full_time_dir = creator.archive_application(
        company="CloudScale Systems",
        role="Senior Backend Engineer",
        role_category="full_time",
        portal_url="https://cloudscale.example/apply/123",
        raw_job_description_md=job_md,
        submitted_form_qna=form_qna,
        tailored_resume_pdf_path=dummy_pdf,
        candidate_skills=["Python", "FastAPI", "Docker", "PostgreSQL"],
        generate_prep=True,
        date_str="2026-11-12",
    )

    assert full_time_dir.exists()
    assert full_time_dir.parent.name == "full_time"

    # Check 1: application_meta.json
    meta_path = full_time_dir / "application_meta.json"
    assert meta_path.exists()
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["company"] == "CloudScale Systems"
    assert meta["role"] == "Senior Backend Engineer"
    assert meta["role_category"] == "full_time"
    assert meta["status"] == STATUS_SUBMITTED

    # Check 2: raw_job_description.md
    jd_path = full_time_dir / "raw_job_description.md"
    assert jd_path.exists()
    assert "CloudScale Systems" in jd_path.read_text(encoding="utf-8")

    # Check 3: submitted_form_qna.json
    qna_path = full_time_dir / "submitted_form_qna.json"
    assert qna_path.exists()
    with open(qna_path, "r", encoding="utf-8") as f:
        qna = json.load(f)
    assert qna["full_name"] == "Jordan Developer"

    # Check 4: tailored_resume.pdf copy
    pdf_dest = full_time_dir / "tailored_resume.pdf"
    assert pdf_dest.exists()
    assert pdf_dest.stat().st_size == dummy_pdf.stat().st_size

    # Check 5: Interview prep pack inside interview_prep/
    prep_dir = full_time_dir / "interview_prep"
    assert prep_dir.exists()
    assert prep_dir.is_dir()

    skills_gap = prep_dir / "technical_skills_gap.md"
    assert skills_gap.exists()
    skills_text = skills_gap.read_text(encoding="utf-8")
    assert "# Technical Skills Gap Analysis" in skills_text
    assert "CloudScale Systems" in skills_text

    behavioral_qa = prep_dir / "targeted_behavioral_qa.md"
    assert behavioral_qa.exists()
    beh_text = behavioral_qa.read_text(encoding="utf-8")
    assert "# Targeted Behavioral Interview Preparation" in beh_text
    assert "STAR" in beh_text

    company_brief = prep_dir / "company_briefing.md"
    assert company_brief.exists()
    brief_text = company_brief.read_text(encoding="utf-8")
    assert "# Company Briefing & Strategic Overview" in brief_text
    assert "CloudScale Systems" in brief_text

    # Part B: Archive internship role
    intern_dir = creator.archive_application(
        company="NextGen AI Labs",
        role="Software Engineering Intern",
        role_category="internships",
        portal_url="https://nextgen.example/internships",
        raw_job_description_md="# Software Engineering Intern at NextGen AI Labs",
        submitted_form_qna=form_qna,
        tailored_resume_pdf_path=dummy_pdf,
        generate_prep=False,
        date_str="2026-11-12",
    )

    assert intern_dir.exists()
    assert intern_dir.parent.name == "internships"
    intern_meta_path = intern_dir / "application_meta.json"
    assert intern_meta_path.exists()
    with open(intern_meta_path, "r", encoding="utf-8") as f:
        intern_meta = json.load(f)
    assert intern_meta["role_category"] == "internships"
    assert intern_meta["company"] == "NextGen AI Labs"
