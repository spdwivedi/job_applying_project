# JobApplier-Core Roadmap

Project repository: `https://github.com/spdwivedi/job_applying_project.git`

This roadmap tracks all six delivery phases of **JobApplier-Core** and assigns ownership across `@spdwivedi` and `@friend` so feature branches can progress in parallel without merge conflicts.

---

## Phase Overview

| Phase | Name | Status | Primary Focus |
| :--- | :--- | :--- | :--- |
| **Phase 1** | Foundation & Security | **Completed** | Repo hygiene, pre-commit leak guards, vault schemas, Gemini client, RenderCV bridge |
| **Phase 2** | Browser Inspection & Form Mapping | **In Progress** | Playwright / `browser-use` ATS inspection, credential decryption, JD & field mapping |
| **Phase 3** | Interactive HITL Verification Panel | Planned | Local FastAPI backend & HITL UI for human review, edits, and submission approval |
| **Phase 4** | Resume Tailoring Engine via RenderCV | Planned | JD-driven resume selection/reordering and deterministic Typst PDF generation |
| **Phase 5** | Archival & Interview Prep Kit Generation | Planned | Application history snapshots, structured logging, and STAR interview prep kits |
| **Phase 6** | ATS End-to-End Stress Testing | Planned | Sandboxed ATS mocks, dry-run E2E suites, failure recovery, and leak audits |

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

### Phase 2: Browser Inspection & Form Mapping — In Progress

Automated ATS page inspection, safe credential/session management, and structured mapping from `vault/` to live application forms.

- [ ] Build `vault/` loader & Pydantic validator for `master_profile.yaml` (`cv`, `work_authorization`, `story_bank`) (`src/engine/vault_loader.py`) — `@spdwivedi`
- [ ] Implement local `AES-256-GCM` + `Argon2id` credential decryptor for `master_credentials.json` using `JOB_APPLIER_VAULT_KEY` (`src/engine/secrets.py`) — `@spdwivedi`
- [ ] Implement Playwright / `browser-use` session manager loading and persisting per-platform `storageState.json` cookies in `vault/platform_cookies/` (`src/agents/session_manager.py`) — `@friend`
- [ ] Build DOM form field & label inspector extracting inputs, selects, radios, checkboxes, file uploaders, and custom screening questions (`src/agents/form_inspector.py`) — `@friend`
- [ ] Wire `GeminiClient.parse_job_description` and `GeminiClient.map_form_field` into an end-to-end mapping orchestrator (`src/engine/mapper.py`) — `@spdwivedi`
- [ ] Add STAR `story_bank` retrieval and response drafting for open-ended screening questions (`src/engine/story_matcher.py`) — `@spdwivedi`
- [ ] Create initial ATS adapters for Greenhouse and Lever application flows (`src/agents/adapters/`) — `@friend`

---

### Phase 3: Interactive HITL Verification Panel

Local Human-in-the-Loop (HITL) control plane ensuring zero blind submissions.

- [ ] Stand up FastAPI backend (`API_PORT=8000`) with run-state persistence in local SQLite (`src/ui/api.py`) — `@friend`
- [ ] Build interactive verification dashboard (`UI_PORT=8501`) showing extracted JD summary, form fields, proposed values, and source attribution (`src/ui/app.py`) — `@friend`
- [ ] Implement inline edit, override, and mandatory human-approval gate before `src/agents/` executes final form fill/submit — `@friend`
- [ ] Add human-escalation pause/resume hooks for MFA prompts, CAPTCHA challenges, and low-confidence fields — `@spdwivedi`
- [ ] Expose tailored resume preview & field-diff summary endpoints between `src/engine/` and `src/ui/` — `@spdwivedi`

---

### Phase 4: Resume Tailoring Engine via RenderCV

Truthful, JD-aligned resume customization compiled deterministically via Typst.

- [ ] Add reusable RenderCV theme/layout presets in `templates/` (e.g., `classic.yaml`, `engineering_compact.yaml`) with zero PII — `@spdwivedi`
- [ ] Build Gemini-assisted resume tailoring pipeline (`src/engine/tailor.py`) that selects and reorders existing bullets/skills from `master_profile.yaml` without fabricating experience — `@spdwivedi`
- [ ] Enforce truth-preservation diff check verifying tailored output only references verified `vault/` entities, dates, and metrics — `@spdwivedi`
- [ ] Integrate `compile_resume()` (`src/engine/resume_compiler.py`) with page-count check and automatic overflow diagnostics — `@spdwivedi`
- [ ] Wire tailored PDF artifact attachment into `src/agents/` file-upload steps and `src/ui/` preview pane — `@friend`

---

### Phase 5: Archival & Interview Prep Kit Generation

Durable per-application records and post-submission interview preparation artifacts.

- [ ] Implement application archiver writing structured bundles to `history/full_time/` and `history/internships/` (`<company>_<role>_<date>/`) (`src/engine/archiver.py`) — `@spdwivedi`
- [ ] Configure structured JSON/rotating runtime logging into `logs/` with automatic secret/PII redaction filters (`src/engine/logging_config.py`) — `@friend`
- [ ] Build Interview Prep Kit generator (`src/engine/prep_kit.py`) combining `JobDescriptionSchema`, tailored resume highlights, and matched `story_bank` STAR answers into a markdown prep brief — `@spdwivedi`
- [ ] Add historical application search & status tracker view in the local UI (`src/ui/`) — `@friend`

---

### Phase 6: ATS End-to-End Stress Testing

Validation of reliability, safety gates, and leak prevention before production daily use.

- [ ] Build local mock ATS test fixtures (Greenhouse, Lever, and multi-page Workday-style forms) for deterministic CI/local testing (`tests/fixtures/`) — `@friend`
- [ ] Implement Playwright dry-run mode filling all fields and stopping prior to final submission button click (`src/agents/`) — `@friend`
- [ ] Add stress-test suite covering session expiration, transient network failures, Gemini schema-validation retries, and Typst compile errors (`tests/`) — `@spdwivedi`
- [ ] Expand ATS adapter coverage for Workday, LinkedIn Easy Apply, and Indeed workflows (`src/agents/adapters/`) — `@friend`
- [ ] Run full security audit verifying zero PII/secrets in git history, tracked files, and sanitized `logs/` — `@spdwivedi` & `@friend`

---

## Parallel Branching Strategy

To minimize merge conflicts during Phase 2+:

- **`@spdwivedi` focus (`src/engine/`, `templates/`, `docs/`)**: Vault loading, credential encryption, Gemini mapping/tailoring, RenderCV compilation, and archival/prep-kit engines.
- **`@friend` focus (`src/agents/`, `src/ui/`, `tests/fixtures/`)**: Playwright/`browser-use` automation, ATS DOM adapters, cookie persistence, FastAPI API, and HITL verification UI.
- **Branch naming**: `spdwivedi/<feature-name>` and `<friend>/<feature-name>` merged into protected `main` via Pull Request.
