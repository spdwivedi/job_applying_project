# Contributing to JobApplier-Core

Thank you for contributing to **JobApplier-Core** (`https://github.com/spdwivedi/job_applying_project.git`). Because this project handles sensitive personal data locally, every contributor must follow the branching, commit, environment setup, and security rules below.

---

## 1. Branch & Pull Request Workflow

- **`main` is protected**: Never commit or push directly to `main`.
- **Feature Branches**: Create a dedicated branch from `main` using your contributor namespace:
  - `spdwivedi/<feature-name>` (e.g., `spdwivedi/vault-loader`, `spdwivedi/tailor-engine`)
  - `<friend>/<feature-name>` (e.g., `<friend>/greenhouse-adapter`, `<friend>/hitl-ui`)
- **Pull Requests**:
  - Open a PR targeting `main` and complete all checklist items in `.github/pull_request_template.md`.
  - Keep PRs scoped to one module or phase task so parallel branches merge cleanly.
  - Ensure all local verification checks pass before requesting review.

---

## 2. Conventional Commits Standard

All commit messages must follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```text
<type>(<optional-scope>): <imperative summary>
```

Allowed types:
- `feat:` — New user-facing or engine capability (e.g., `feat(agents): add Greenhouse DOM field extractor`)
- `fix:` — Bug fix (e.g., `fix(compiler): handle empty experience highlights list`)
- `docs:` — Documentation changes (e.g., `docs: update ARCHITECTURE.md data flow`)
- `test:` — Adding or updating tests and fixtures (e.g., `test(llm): add mocked schema validation cases`)
- `chore:` — Tooling, dependencies, or maintenance (e.g., `chore(deps): pin fastapi in requirements.txt`)
- `refactor:` — Code restructuring without changing behavior
- `ci:` — GitHub Actions or hook workflow updates

---

## 3. Local Environment Setup

### Prerequisites
- Python **3.13+** (or compatible Python 3.11+ environment)
- Git (on Windows, Git for Windows with **Git Bash** is required for POSIX pre-commit hooks)
- `uv` (optional, recommended for fast package installation) or `pip`

### Step 1: Clone & Create Virtual Environment

**Windows (PowerShell):**
```powershell
git clone https://github.com/spdwivedi/job_applying_project.git
cd job_applying_project
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux (Bash/Zsh):**
```bash
git clone https://github.com/spdwivedi/job_applying_project.git
cd job_applying_project
python3 -m venv .venv
source .venv/bin/activate
```

### Step 2: Install Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

Or with `uv`:
```bash
uv pip install -r requirements.txt
```

### Step 3: Configure Local Environment Variables (`.env`)

Copy `.env.example` to `.env` and set your own `GEMINI_API_KEY`:

**Windows (PowerShell):**
```powershell
Copy-Item .env.example .env
```

**macOS / Linux:**
```bash
cp .env.example .env
```

Edit `.env` locally and replace `your_gemini_api_key_here` with your real key. **Never commit `.env`.**

### Step 4: Initialize Private `vault/` Files

Create your local, gitignored vault files from the example schemas:

**Windows (PowerShell):**
```powershell
Copy-Item vault/master_profile.yaml.example vault/master_profile.yaml
Copy-Item vault/master_credentials.example.json vault/master_credentials.json
```

**macOS / Linux:**
```bash
cp vault/master_profile.yaml.example vault/master_profile.yaml
cp vault/master_credentials.example.json vault/master_credentials.json
```

Populate `vault/master_profile.yaml` and `vault/master_credentials.json` with your personal details locally. Both files reside inside gitignored `vault/`.

### Step 5: Install Security Pre-Commit Hooks (Mandatory)

Every contributor **must** install the pre-commit security hook immediately after cloning so secrets and PII are blocked locally before any commit is created.

**Windows (PowerShell):**
```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_hooks.ps1
```

**macOS / Linux:**
```bash
bash scripts/setup_hooks.sh
```

### Step 6: Verify Local Setup

Run a quick sanity check inside your activated `.venv`:
```bash
python -c "import src.engine.llm, src.engine.resume_compiler; print('Engine imports OK')"
rendercv --version
```

---

## 4. Security & PII Hygiene Rules

The repository pre-commit hook (`.git/hooks/pre-commit`, installed from `scripts/hooks/pre-commit`) will **hard-block** any commit that contains:

1. **Protected paths**: Any file inside `vault/`, `history/`, `logs/`, `.env`, `.env.*` (other than `.env.example`), `*.sqlite`, `*.db`, or `storageState.json`.
2. **API keys & tokens**: Strings matching `AIzaSy...` or `sk-...`.
3. **Email addresses**: Do **not** put real or synthetic email addresses (anything matching `<local>@<domain>.<tld>`) in code, tests, or Markdown docs. Use placeholders like `<your-email>` or `EMAIL_PLACEHOLDER`.
4. **Phone numbers**: Do **not** put real or synthetic phone numbers in tracked files. Use placeholders like `PHONE_PLACEHOLDER`.

Never bypass the hook with `--no-verify`. If the hook flags a file, remove the sensitive pattern or move the private artifact into `vault/`, `history/`, or `logs/`.
