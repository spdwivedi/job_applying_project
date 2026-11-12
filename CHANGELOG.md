# Changelog

All notable changes to **JobApplier-Core** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Phase 2 browser inspection and ATS form mapping (`src/agents/`).
- Encrypted `vault/` credential loader and `storageState.json` session manager.
- Phase 3 FastAPI Human-in-the-Loop (HITL) verification UI (`src/ui/`).

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
