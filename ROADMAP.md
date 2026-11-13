# JobApplier-Core Roadmap

Project repository: `https://github.com/spdwivedi/job_applying_project.git`

This roadmap tracks the delivery phases of **JobApplier-Core** and assigns ownership across `@spdwivedi` and `@friend` so feature branches can progress in parallel without merge conflicts.

---

## Phase Overview

| Phase | Name | Status | Primary Focus |
| :--- | :--- | :--- | :--- |
| **Phase 1** | Foundation & Security | **Completed** | Repo hygiene, pre-commit leak guards, vault schemas, Gemini client, RenderCV bridge |
| **Phase 2** | Browser Inspection & Form Mapping | **Completed** | Playwright ATS inspection, DOM parser, job profiler, STAR answer crafter, safe browser driver |
| **Phase 3** | Interactive HITL Verification Panel | **Completed** | Local FastAPI review dashboard (`src/ui/app.py`), field overrides, and strict approval gate |
| **Phase 4** | Real-World ATS Adapters & Provenance Guard | **Completed** | Greenhouse, Lever & Workday wizard adapters, multi-page forms, anti-hallucination fact guard |
| **Phase 5** | Auth Persistence & Pipeline Tracker | **Completed** | Stealth session cookies (`platform_cookies/`), centralized pipeline tracker, batch intake checker |
| **Phase 6** | Resilience, Ashby & Page-Budget Engine | **Completed** | CAPTCHA/bot-challenge interception & WebSocket alerts, Ashby ATS adapter, RenderCV page-budget enforcer |
| **Phase 7** | Autonomous Discovery, Fit Scoring & Interview Practice | **Completed** | Zero-token Greenhouse/Lever feed scanners, 1–5 fit scoring & hard-blocker filter, interactive STAR practice |
| **Phase 8** | ATS End-to-End Stress Testing & Backlog | Planned | Sandboxed ATS staging mocks, dry-run E2E suites, failure recovery, and leak audits |

---

## Detailed Phase Breakdown & Feature Checklist

### Phase 1: Foundation & Security — Completed

Core workspace scaffolding, strict PII/secret protection, local data schemas, and foundational engine modules.

- [x] Initialize Git repository, `.venv`, and modular directory structure (`src/agents/`, `src/engine/`, `src/ui/`, `templates/`, `docs/`, `scripts/`, gitignored `vault/`, `history/`, `logs/`) — `@spdwivedi`
- [x] Configure `.gitignore` to exclude `vault/`, `history/`, `logs/`, `.env`, `.env.*` (except `.env.example`), `*.sqlite`, `*.db`, `storageState.json`, `__pycache__/`, and `.venv/` — `@spdwivedi`
- [x] Implement pre-commit security hook (`scripts/hooks/pre-commit`, `.pre-commit-config.yaml`) and cross-platform installers (`scripts/setup_hooks.ps1`, `scripts/setup_hooks.sh`) blocking API keys (`AIzaSy...`, `sk-...`), emails, phone numbers, and protected directory leaks — `@spdwivedi`
- [x] Add `.env.example` defining `UI_PORT`, `API_PORT`, `VAULT_PATH`, `TEMPLATES_PATH`, `HISTORY_PATH`, `STORAGE_STATE_PATH`, and `GEMINI_API_KEY` — `@spdwivedi`
- [x] Define gitignored `vault/` schemas (`master_profile.yaml.example`, `master_profile.yaml`, `master_credentials.example.json`) compatible with RenderCV v2.8 (`cv` + `design`), `work_authorization`, and STAR `story_bank` — `@spdwivedi`
- [x] Implement Gemini LLM client harness (`src/engine/llm.py`) supporting `gemini-1.5-pro` and `gemini-1.5-flash` with Pydantic v2 schemas (`JobDescriptionSchema`, `FormFieldMappingSchema`) and `MissingGeminiCredentialsError` validation — `@spdwivedi`
- [x] Implement RenderCV Typst compiler bridge (`src/engine/resume_compiler.py`) with `ensure_rendercv_installed()` and isolated `compile_resume(yaml_path, output_pdf_path)` PDF validation — `@spdwivedi`
- [x] Author project governance docs (`ROADMAP.md`, `CHANGELOG.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`) and `.github/pull_request_template.md` — `@spdwivedi`

---

### Phase 2: Browser Inspection & Form Mapping — Completed

Automated ATS page inspection, DOM form extraction heuristics, job profiling, grounded STAR answering, and safe browser driving.

- [x] Implement `JobProfilerAgent` and `JobProfileSchema` for scraping and structured requirement extraction from URL or raw text (`src/agents/job_profiler.py`) — `@spdwivedi`
- [x] Build DOM form parser (`src/engine/form_parser.py`) with `FormField` and `FormSchema` models extracting inputs, selects, radios, checkboxes, textareas, and resume file controls — `@friend`
- [x] Implement Playwright `FormInspectorAgent` with session state support from cookies (`src/agents/form_inspector.py`) — `@friend`
- [x] Implement `AnswerCrafterAgent` mapping profile attributes and drafting grounded STAR answers for open-ended screening questions (`src/agents/answer_crafter.py`) — `@spdwivedi`
- [x] Implement `ResumeTailorAgent` reordering skills/highlights and compiling tailored PDFs via RenderCV (`src/agents/resume_tailor.py`) — `@spdwivedi`
- [x] Implement `BrowserDriverAgent` filling form controls, attaching resumes, blocking auto-submits, and requiring explicit human approval (`src/agents/browser_driver.py`) — `@spdwivedi`

---

### Phase 3: Interactive HITL Verification Panel — Completed

Local Human-in-the-Loop (HITL) control plane ensuring zero blind submissions.

- [x] Stand up local FastAPI dashboard (`src/ui/app.py`) with in-memory `HITLReviewState` session manager — `@friend`
- [x] Build responsive verification template (`src/ui/templates/review.html`) with job metadata badges, editable form fields, and embedded resume PDF preview — `@friend`
- [x] Implement control buttons: "Save & Re-fill Fields", "Approve & Submit" (strict token verification gate), and "Reject / Cancel" (`src/ui/app.py`) — `@friend`
- [x] Implement `DossierCreatorAgent` archiving applications into permanent directories with metadata, raw JD, Q&A records, and synthesized interview prep kits (`src/agents/dossier_creator.py`) — `@spdwivedi`
- [x] Build automated integration test suite (`tests/test_phase2_phase3.py`) verifying all Phase 2 and Phase 3 capabilities with 100% pass rate — `@spdwivedi`

---

### Phase 4: Real-World ATS Adapters & Provenance Guard — Completed

Specialized ATS interaction adapters, multi-page form wizards, and truth-grounded resume provenance verification.

- [x] Implement specialized portal adapters for Greenhouse, Lever, and Workday with multi-page wizard support (`src/engine/ats_adapters.py`) — `@spdwivedi`
- [x] Build anti-hallucination fact auditor and draft sanitizer verifying metrics, companies, dates, and degrees against master profile (`src/engine/fact_verifier.py`) — `@friend`
- [x] Wire ATS portal adapters to detect intermediate Next/Continue buttons vs final Submit buttons, halting automatically before final submit — `@spdwivedi`

---

### Phase 5: Auth Persistence & Pipeline Tracker — Completed

Durable application lifecycle tracking, authenticated stealth browser sessions, and batch intake validation.

- [x] Build stealth browser context manager with anti-detection evasions and headed manual authentication exporter (`src/engine/session_manager.py`) — `@spdwivedi`
- [x] Implement local JSON/SQLite pipeline tracker (`history/tracker.json`) and REST endpoints (`src/engine/pipeline_tracker.py`, `src/ui/tracker_view.py`) — `@friend`
- [x] Build modern interactive tracker dashboard tab with funnel metrics, status transition dropdowns, and interview calendar (`src/ui/templates/tracker.html`) — `@friend`
- [x] Implement batch URL intake and job liveness checker filtering out closed or expired job postings (`src/agents/batch_scanner.py`) — `@spdwivedi`
- [x] Author comprehensive integration test suite (`tests/test_phase4_phase5.py`) verifying all Phase 4 and Phase 5 capabilities with 100% pass rate — `@spdwivedi`
- [ ] Expose tailored resume preview & field-diff summary endpoints between `src/engine/` and `src/ui/` — `@spdwivedi`

---

### Phase 6: Resilience, Ashby & Page-Budget Engine — Completed

Runtime security-challenge interception, Ashby React ATS support, and deterministic one-page RenderCV compaction.

- [x] Implement CAPTCHA & bot-challenge detector (`src/engine/challenge_handler.py`) inspecting Playwright DOM and child frames for Cloudflare Turnstile, Google reCAPTCHA, hCaptcha, and Workday/generic challenges, emitting `EVENT_SECURITY_CHALLENGE_DETECTED`, pausing without closing the browser context, and waiting for human resolution — `@spdwivedi`
- [x] Build FastAPI WebSocket notification bridge and HITL alert banner (`src/ui/challenge_ws.py`, `src/ui/app.py`, `src/ui/templates/review.html`) pushing `"CAPTCHA Detected: Complete in Browser"` over `/ws/challenges` with status/resolve endpoints — `@friend`
- [x] Implement Ashby ATS portal adapter (`src/engine/ats_ashby.py`) with `AshbyAdapter` detection, `_ashby_` field extraction, custom select/multi-select/radiogroup/dropzone filling, multi-step navigation, and strict final submit blocking — `@spdwivedi`
- [x] Implement RenderCV v2.8 Typst page-budget enforcer (`src/engine/page_budget.py`) using `pypdf` (`get_pdf_page_count`, `enforce_page_budget`) to tighten theme spacing/typography and trim lowest-relevance bullets to fit target page limits — `@spdwivedi`

---

### Phase 7: Autonomous Discovery, Fit Scoring & Interview Practice — Completed

Zero-token ATS job discovery, calibrated hard-blocker filtering, and dossier-grounded STAR interview simulation.

- [x] Configure target company feed manifest (`config/companies.yml`) and zero-token public Greenhouse/Lever ATS feed scanners (`src/intake/__init__.py`, `src/intake/ats_scanner.py`) with `max_age_days` staleness filtering, keyword filters, deduplication, and atomic `intake_queue.json` persistence — `@spdwivedi`
- [x] Build intelligent fit scoring & hard-blocker engine (`src/engine/fit_scorer.py`) rejecting visa sponsorship, citizenship, security clearance, country authorization, and strict years-of-experience conflicts while assigning calibrated 1–5 fit scores (`skip` / `review` / `priority_apply`) and updating `intake_queue.json` — `@spdwivedi`
- [x] Implement interactive interview practice simulator (`src/agents/interview_practice.py`) loading `interview_prep/targeted_behavioral_qa.md` and `technical_skills_gap.md` dossiers, iterating sequential questions via API/CLI, evaluating answers across STAR dimensions, and recommending profile-grounded `suggested_evidence` — `@friend`
- [x] Author comprehensive Phase 6 & Phase 7 integration test suite (`tests/test_phase6_phase7.py`) verifying challenge interception/WS alerts, Ashby adapter, RenderCV page-budget compaction, Greenhouse/Lever intake feeds, fit scoring & hard blockers, and interactive STAR interview practice — `@spdwivedi` & `@friend`

---

### Phase 8: ATS End-to-End Stress Testing & Backlog — Planned

Validation of reliability, safety gates, additional portal adapters, and leak prevention before autonomous daily operation.

- [ ] Expose tailored resume preview & field-diff summary endpoints between `src/engine/` and `src/ui/` — `@spdwivedi`
- [ ] Add reusable RenderCV theme/layout presets in `templates/` (e.g., `classic.yaml`, `engineering_compact.yaml`) with zero PII — `@spdwivedi`
- [ ] Configure structured JSON/rotating runtime logging into `logs/` with automatic secret/PII redaction filters (`src/engine/logging_config.py`) — `@friend`
- [ ] Build local mock ATS test fixtures and sandboxed staging E2E suites for session expiration, transient network failures, Gemini schema-validation retries, and Typst compile errors (`tests/fixtures/`, `tests/`) — `@spdwivedi` & `@friend`
- [ ] Implement Playwright dry-run mode filling all fields across multi-page flows and halting prior to final submission button click (`src/agents/`) — `@friend`
- [ ] Expand ATS adapter coverage for LinkedIn Easy Apply and Indeed workflows (`src/agents/adapters/`) — `@friend`
- [ ] Run full security audit verifying zero PII/secrets in git history, tracked files, and sanitized `logs/` — `@spdwivedi` & `@friend`

---

## Parallel Branching Strategy

To minimize merge conflicts during Phase 2+:

- **`@spdwivedi` focus (`src/engine/`, `templates/`, `docs/`)**: Vault loading, credential encryption, Gemini mapping/tailoring, RenderCV compilation, and archival/prep-kit engines.
- **`@friend` focus (`src/agents/`, `src/ui/`, `tests/fixtures/`)**: Playwright/`browser-use` automation, ATS DOM adapters, cookie persistence, FastAPI API, and HITL verification UI.
- **Branch naming**: `spdwivedi/<feature-name>` and `<friend>/<feature-name>` merged into protected `main` via Pull Request.
