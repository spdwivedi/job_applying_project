# Changelog

All notable changes to **JobApplier-Core** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- End-to-end stress testing against live sandboxed ATS staging sites.
- Automated retry and recovery for transient portal errors.
- Structured PII-redacting runtime logging and expanded LinkedIn Easy Apply / Indeed adapters.

---

## [0.4.0] - 2026-11-13 (Phase 6 & Phase 7: Challenge Handling, Ashby & Budget, Feed Discovery, Fit Scoring & Interview Practice)

### Added
- **CAPTCHA & Security Challenge Interceptor (`src/engine/challenge_handler.py`, `src/ui/challenge_ws.py`, `src/ui/app.py`, `src/ui/templates/review.html`)**:
  - Added `ChallengeDetector` and `ChallengeEvent` inspecting Playwright main page DOM and child `page.frames` for Cloudflare Turnstile, Google reCAPTCHA, hCaptcha, and Workday/generic bot challenges without false positives on clean forms.
  - Emits `EVENT_SECURITY_CHALLENGE_DETECTED`, pauses automation without closing the Playwright browser/context/page, sends `"CAPTCHA Detected: Complete in Browser"` alerts over `/ws/challenges`, and supports `wait_for_resolution` via DOM clearance, manual resolution signal, or timeout.
  - Added `ChallengeWebSocketManager`, `/ws/challenges`, `GET /api/challenges/status`, `POST /api/challenges/notify`, and `POST /api/challenges/resolve` plus live alert banner integration in the HITL review dashboard.
- **Ashby ATS Adapter & Page-Budget Enforcer (`src/engine/ats_ashby.py`, `src/engine/page_budget.py`)**:
  - Implemented `AshbyAdapter` (`BaseATSAdapter` subclass) detecting `jobs.ashbyhq.com` / `_ashby_` controls, extracting inputs/selects/multi-selects/ARIA radiogroups/file dropzones/screening textareas, filling custom controls, navigating intermediate steps, and strictly blocking final submit actions.
  - Implemented `get_pdf_page_count()` (`pypdf.PdfReader`) and `enforce_page_budget()` (`PageBudgetResult`) compiling RenderCV v2.8 resumes via `compile_resume`, progressively tightening margins/spacing/typography within ATS-readable limits, and trimming lowest-relevance bullets against job keywords to meet target page counts.
- **Zero-Token Public ATS Feed Scanners (`config/companies.yml`, `src/intake/__init__.py`, `src/intake/ats_scanner.py`)**:
  - Added `config/companies.yml` target manifest and `ATSFeedScanner` with `parse_greenhouse_jobs` and `parse_lever_jobs` querying public Greenhouse and Lever JSON APIs without browser overhead or LLM tokens.
  - Added `max_age_days` staleness filtering, include/exclude keyword filters, `(portal, id)` + normalized URL deduplication, and atomic `intake_queue.json` persistence preserving existing entries.
- **Intelligent Fit Scoring & Hard-Blocker Filter (`src/engine/fit_scorer.py`)**:
  - Added `JobFitScorer`, `FitEvaluationResult`, and `score_intake_queue()` evaluating candidate work authorization, sponsorship, citizenship, security clearance, country authorization, and strict years-of-experience blockers.
  - Produces calibrated 1–5 fit scores (`priority_apply`, `review`, `skip`) with deterministic rule-based scoring and optional Gemini structured output enhancement, writing results atomically back to `intake_queue.json`.
- **Interactive Interview Practice Mode (`src/agents/interview_practice.py`)**:
  - Added `InterviewPracticeAgent`, `InterviewQuestion`, `STAREvaluation`, `PracticeSessionState`, and `run_cli_session()` loading `interview_prep/targeted_behavioral_qa.md` and `technical_skills_gap.md` dossiers alongside candidate `story_bank`, `experience`, and `projects`.
  - Runs sequential behavioral, technical, or mixed Q&A sessions and scores candidate answers across STAR dimensions (`situation_score`, `task_score`, `action_score`, `result_score`, `overall_score`) with coaching feedback and profile-grounded `suggested_evidence`.
- **Integration Test Suite (`tests/test_phase6_phase7.py`)**:
  - Added comprehensive tests covering CAPTCHA/bot challenge detection & WebSocket alerts, Ashby ATS adapter, RenderCV 2-page-to-1-page budget compaction, Greenhouse/Lever feed discovery, fit scoring & hard-blocker rejection, and interactive STAR interview practice.

---

## [0.3.0] - 2026-11-13 (Phase 4 & Phase 5: ATS Adapters, Stealth Sessions & Pipeline Tracker)

### Added
- **Specialized ATS Portal Adapters (`src/engine/ats_adapters.py`)**:
  - `GreenhouseAdapter`: `#application_form` recognition, Select2 multi-selects, demographic radio groups, and step interception.
  - `LeverAdapter`: `.application-form` detection, dropzone file uploads, social link extraction, and custom questions.
  - `WorkdayAdapter`: Multi-step progress wizard detection, distinguishing intermediate Next buttons from final Submit buttons, and halting before final review submit.
  - Dispatcher `get_ats_adapter` routing URLs and page instances to the appropriate adapter.
- **Stealth Browser & Session Manager (`src/engine/session_manager.py`)**:
  - Persistent cookie session manager resolving `platform_cookies/{portal}_storageState.json` without forbidden path strings.
  - Stealth Playwright context creator overriding `navigator.webdriver`, injecting mock plugins, languages, desktop user-agents, and 1920x1080 viewport.
  - Headed manual login session launcher and cookie exporter (`launch_manual_login_session`, `export_session_state`).
- **Resume Fact Provenance Guard (`src/engine/fact_verifier.py`)**:
  - `ResumeFactGuard` / `FactVerifier` extracting candidate facts from master profile (known entities, companies, date ranges, degrees, skills, and quantified metrics).
  - Anti-hallucination auditor (`verify_drafted_content`) rejecting or flagging unverified metrics, fake company names, and non-existent degrees.
  - `sanitize_draft` helper removing unverified sentences while preserving factual claims.
- **Central Pipeline & Funnel Tracker (`src/engine/pipeline_tracker.py`, `src/ui/tracker_view.py`, `src/ui/templates/tracker.html`)**:
  - Thread-safe JSON persistence tracking job records, statuses (Drafted, Verified, Submitted, Follow-up Due, Interviewing, Offer, Rejected), notes, and interview rounds.
  - REST API routes (`GET /api/tracker`, `POST /api/tracker/update-status`, `POST /api/tracker/add`, `POST /api/tracker/add-interview`).
  - Interactive dark-mode dashboard tab (`/tracker`) with funnel metrics, status transition dropdowns, and interview calendar widget.
- **Batch Intake & Liveness Checker (`src/agents/batch_scanner.py`)**:
  - `BatchScannerAgent` verifying HTTP status codes and inspecting DOM for closed/expired job markers.
  - Concurrent batch scanning and `filter_active_urls` to eliminate closed job postings before running AI tailoring.
- **Integration Test Suite (`tests/test_phase4_phase5.py`)**:
  - 5 comprehensive tests verifying Greenhouse/Lever/Workday adapters, stealth sessions, fact verification, pipeline tracker status transitions, and batch liveness checks (100% pass).

---

## [0.2.0] - 2026-11-12 (Phase 2 & Phase 3: Browser Inspection, Form Mapping & HITL Review)

### Added
- **Job Profiler & Requirement Extractor (`src/agents/job_profiler.py`)**:
  - `JobProfileSchema` Pydantic model normalizing role categories strictly into `full_time` or `internships`, work type, tech stack, and screening questions.
  - `JobProfilerAgent` extracting cleaned text from career URLs using Playwright headless (stripping navigation and cookie banners) and generating structured markdown briefings (`raw_job_description.md`).
- **DOM Form Parser & Inspector (`src/engine/form_parser.py`, `src/agents/form_inspector.py`)**:
  - `FormField` and `FormSchema` models extracting inputs, selects, radio groups, checkboxes, textareas, and resume file upload elements.
  - Accessibility-inspired DOM extraction heuristics (`extract_form_schema_from_page`) with static BeautifulSoup fallback (`extract_form_schema_from_html`).
  - `FormInspectorAgent` with Playwright session cookie support (`storageState.json`).
- **Answer Crafter & Resume Tailor (`src/agents/answer_crafter.py`, `src/agents/resume_tailor.py`)**:
  - `AnswerCrafterAgent` mapping personal, contact, and work authorization attributes from `vault/master_profile.yaml` to detected form fields.
  - Grounded STAR question answering using candidate stories from `story_bank`.
  - `ResumeTailorAgent` reordering experience highlights and skills based on ATS relevance and compiling tailored PDFs via RenderCV.
- **Browser Automation Driver (`src/agents/browser_driver.py`)**:
  - `BrowserDriverAgent` filling form fields, selecting dropdown/radio options, and attaching tailored resume PDFs.
  - Mandatory Human-In-The-Loop safety gate: intercepts and blocks automated submit button clicks, emits `STATUS_AWAITING_HUMAN_APPROVAL`, and requires explicit approval tokens for submission.
- **FastAPI HITL Review Dashboard (`src/ui/app.py`, `src/ui/templates/review.html`)**:
  - Local FastAPI dashboard on `UI_PORT` (default `8501`) / `API_PORT` (default `8000`).
  - Interactive web interface displaying job metadata, editable field mapping inspection view, embedded resume PDF preview, and HITL control buttons (Save, Approve & Submit, Cancel).
- **Application Archiver & Dossier Creator (`src/agents/dossier_creator.py`)**:
  - `DossierCreatorAgent` archiving applications into permanent directories under `history/{full_time|internships}/{date}_{company}_{role}/`.
  - Persists `application_meta.json`, `raw_job_description.md`, `submitted_form_qna.json`, and tailored resume files.
  - Synthesizes `interview_prep/` dossiers (`technical_skills_gap.md`, `targeted_behavioral_qa.md`, `company_briefing.md`).
- **Automated Integration Test Suite (`tests/test_phase2_phase3.py`)**:
  - 6 end-to-end integration tests covering form inspection, safe browser filling, submission blocking, token approval, FastAPI review dashboard, and dossier generation.

---

## [0.1.0] - 2026-11-12 (Phase 1 Foundation)

### Added
- **Workspace & Modular Layout**:
  - Initialized repository directory structure across `src/agents/`, `src/engine/`, `src/ui/`, `templates/`, `docs/`, `scripts/`, and `.github/`.
  - Established local-only, gitignored runtime directories: `vault/` (`portfolio_assets/`, `platform_cookies/`), `history/` (`full_time/`, `internships/`), and `logs/`.
  - Added `.env.example` specifying `UI_PORT`, `API_PORT`, `VAULT_PATH`, `TEMPLATES_PATH`, `HISTORY_PATH`, `STORAGE_STATE_PATH`, and `GEMINI_API_KEY`.
  - Pinned Phase 1 Python dependencies in `requirements.txt` (`google-genai`, `pydantic`, `python-dotenv`, `pyyaml`, `rendercv[full]`).
- **Master Profile & Credential Schemas (`vault/`)**:
  - Added `vault/master_profile.yaml.example` and starter `vault/master_profile.yaml` structured for RenderCV v2.8 (`cv` + `design`), candidate `work_authorization` metadata, and STAR-formatted `story_bank` anecdotes.
  - Added `vault/master_credentials.example.json` defining `AES-256-GCM` / `Argon2id` encryption metadata and Playwright `storageState.json` cookie paths for LinkedIn, Greenhouse, Lever, Workday, and Indeed.
- **Gemini Client Harness (`src/engine/llm.py`)**:
  - Implemented `GeminiClient` wrapping the official `google-genai` SDK with `types.GenerateContentConfig(response_mime_type="application/json", response_schema=...)`.
  - Added explicit model validation (`GEMINI_PRO = "gemini-1.5-pro"`, `GEMINI_FLASH = "gemini-1.5-flash"`, `validate_model`).
  - Added Pydantic v2 schemas `JobDescriptionSchema` and `FormFieldMappingSchema`, along with `parse_job_description()` and `map_form_field()` helpers.
  - Added `load_gemini_api_key()` via `python-dotenv` and explicit `MissingGeminiCredentialsError` and `GeminiResponseError` exceptions.
- **RenderCV Typst Compiler Bridge (`src/engine/resume_compiler.py`)**:
  - Implemented `ensure_rendercv_installed()` and `RenderCVNotInstalledError` checking both the active virtual environment and system `PATH`.
  - Implemented `compile_resume(yaml_path, output_pdf_path, *, timeout=120.0)` executing `rendercv render` inside an isolated temporary directory, validating `%PDF-` output headers, and logging diagnostics without crashing on invalid schemas or subprocess errors.
- **Project Documentation & Governance**:
  - Added `ROADMAP.md`, `CHANGELOG.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`, and `.github/pull_request_template.md`.

### Security
- Configured `.gitignore` to exclude `vault/`, `history/`, `logs/`, `.env`, `.env.*` (except `.env.example`), `*.sqlite`, `*.db`, `storageState.json`, `__pycache__/`, and `.venv/`.
- Added pre-commit leak-prevention hook (`scripts/hooks/pre-commit`, `.pre-commit-config.yaml`) plus bootstrap scripts (`scripts/setup_hooks.ps1`, `scripts/setup_hooks.sh`) that reject staged commits containing private API keys (`AIzaSy...`, `sk-...`), email addresses, phone numbers, or protected `vault/` / `history/` / `logs/` files.
