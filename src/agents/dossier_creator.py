"""Application archiver and interview dossier synthesizer for JobApplier-Core.

Archives submitted job applications with immutable metadata snapshots, raw job descriptions,
submitted form Q&A records, and tailored resume artifacts. Synthesizes targeted interview
preparation packs (technical skills gap, targeted behavioral Q&A, company briefing).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal

logger = logging.getLogger(__name__)

DEFAULT_ROLE_CATEGORY: Final[str] = "full_time"
SUPPORTED_ROLE_CATEGORIES: Final[tuple[str, str]] = ("full_time", "internships")
STATUS_SUBMITTED: Final[str] = "Submitted"

# Known technical keywords for heuristic extraction when offline or during fallback
COMMON_TECH_KEYWORDS: Final[tuple[str, ...]] = (
    "python",
    "typescript",
    "javascript",
    "go",
    "rust",
    "java",
    "c++",
    "c#",
    "kotlin",
    "swift",
    "sql",
    "bash",
    "fastapi",
    "django",
    "flask",
    "react",
    "next.js",
    "node.js",
    "express",
    "vue",
    "angular",
    "pytorch",
    "tensorflow",
    "pandas",
    "numpy",
    "scikit-learn",
    "docker",
    "kubernetes",
    "terraform",
    "aws",
    "gcp",
    "azure",
    "ci/cd",
    "github actions",
    "postgresql",
    "mysql",
    "redis",
    "mongodb",
    "elasticsearch",
    "dynamodb",
    "sqlite",
    "microservices",
    "graphql",
    "grpc",
    "rest",
    "distributed systems",
    "kafka",
    "rabbitmq",
    "pytest",
    "playwright",
    "selenium",
    "git",
)


def sanitize_slug(company: str, role: str, date_str: str | None = None) -> str:
    """Sanitize company and role names into a clean dossier slug.

    Format: YYYY-MM-DD_company_role

    Args:
        company: Hiring organization name.
        role: Target job title.
        date_str: Optional YYYY-MM-DD date override; defaults to current UTC date.

    Returns:
        Clean, filesystem-safe slug string.
    """
    if date_str is None or not date_str.strip():
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        date_str = date_str.strip()

    def _clean_token(text: str) -> str:
        normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", normalized.strip().lower())
        return cleaned.strip("_")

    clean_company = _clean_token(company) or "company"
    clean_role = _clean_token(role) or "role"
    return f"{date_str}_{clean_company}_{clean_role}"


def normalize_role_category(role_category: str | None) -> str:
    """Normalize role category to either 'full_time' or 'internships'.

    Args:
        role_category: Input role classification string.

    Returns:
        Either 'full_time' or 'internships'.
    """
    if not role_category or not isinstance(role_category, str):
        return DEFAULT_ROLE_CATEGORY

    lowered = role_category.strip().lower().replace("-", "_").replace(" ", "_")
    if "intern" in lowered:
        return "internships"
    return "full_time"


def _extract_tech_keywords_from_text(text: str) -> list[str]:
    """Extract recognized technology keywords mentioned in job description text."""
    if not text:
        return []
    lowered = text.lower()
    found: list[str] = []
    for kw in COMMON_TECH_KEYWORDS:
        # Use word boundaries where applicable
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, lowered):
            found.append(kw)
    return found


def _generate_markdown_with_gemini(
    gemini_client: Any | None,
    prompt: str,
    system_instruction: str = "",
) -> str | None:
    """Invoke Gemini API to generate markdown text, returning None on failure."""
    if gemini_client is None:
        return None

    try:
        # 1. Official GeminiClient instance wrapping genai.Client
        if hasattr(gemini_client, "client") and hasattr(gemini_client.client, "models"):
            model = getattr(gemini_client, "default_model", "gemini-1.5-flash")
            config = None
            if system_instruction:
                try:
                    from google.genai import types  # type: ignore[import-not-found]

                    config = types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.2,
                    )
                except Exception:
                    config = None

            response = gemini_client.client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
            text = getattr(response, "text", None)
            if isinstance(text, str) and text.strip():
                return text.strip()

        # 2. Direct generate_content callable
        if hasattr(gemini_client, "generate_content") and callable(gemini_client.generate_content):
            resp = gemini_client.generate_content(prompt)
            if isinstance(resp, str) and resp.strip():
                return resp.strip()
            text = getattr(resp, "text", None)
            if isinstance(text, str) and text.strip():
                return text.strip()

        # 3. Direct generate_text or generate callable
        for method_name in ("generate_text", "generate"):
            if hasattr(gemini_client, method_name) and callable(getattr(gemini_client, method_name)):
                resp = getattr(gemini_client, method_name)(prompt)
                if isinstance(resp, str) and resp.strip():
                    return resp.strip()
                text = getattr(resp, "text", None)
                if isinstance(text, str) and text.strip():
                    return text.strip()

    except Exception as exc:
        logger.warning("Gemini markdown generation call failed: %s; using structured template", exc)

    return None


def _build_skills_gap_template(
    company: str,
    role: str,
    job_description_text: str,
    candidate_skills: list[str] | None = None,
) -> str:
    """Synthesize structured technical skills gap analysis markdown document."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    detected_tech = _extract_tech_keywords_from_text(job_description_text)
    known_skills = [s.strip().lower() for s in (candidate_skills or []) if s.strip()]

    if detected_tech:
        tech_list_md = "\n".join(f"- `{tech.capitalize()}`" for tech in detected_tech)
    else:
        tech_list_md = "- Core modern software engineering stack\n- Cloud infrastructure & backend services\n- Relational & document storage systems"

    matrix_rows: list[str] = []
    if detected_tech:
        for tech in detected_tech:
            name = tech.capitalize()
            is_matched = any(tech in s or s in tech for s in known_skills) if known_skills else True
            prof = "Proficient / Verified" if is_matched else "Familiar / Gap"
            priority = "Review Architecture" if is_matched else "High Priority Revision"
            matrix_rows.append(f"| {name} | Required | {prof} | {priority} |")
    else:
        matrix_rows.append("| Distributed Architecture | Core | Proficient | Review Concurrency & Latency |")
        matrix_rows.append("| Database Storage & Indexing | Core | Proficient | Review Query Optimization |")
        matrix_rows.append("| Cloud & Containerization | Required | Proficient | Review Deployment Workflows |")

    table_body = "\n".join(matrix_rows)

    return f"""# Technical Skills Gap Analysis

**Target Role:** {role}  
**Hiring Company:** {company}  
**Generated Date:** {date_str}

---

## 1. Executive Summary & Tech Stack Overview

This report provides a targeted technical evaluation of the job requirements against the candidate profile to optimize pre-interview preparation and technical screening performance.

### Target Technology Stack Identified in Job Posting
{tech_list_md}

---

## 2. Skills Match & Gap Matrix

| Technology / Domain | Requirement Status | Candidate Profile | Priority Focus |
| :--- | :--- | :--- | :--- |
{table_body}

---

## 3. Prioritized Study Focus Areas

### High Priority: Core Architectural Concepts
- **System Scalability & Concurrency:** Review asynchronous event loops, connection pooling, and horizontal scaling patterns.
- **Data Persistence & Caching:** Deep-dive into cache invalidation strategies (e.g., write-through vs cache-aside), database indexing, and query execution plans.
- **Reliability & Fault Tolerance:** Circuit breaker patterns, exponential backoff with jitter, idempotency keys, and graceful degradation.

### Medium Priority: Framework-Specific Nuances
- Review language-specific runtime internals (memory management, concurrency models, garbage collection).
- RESTful and asynchronous API contracts, validation pipelines, and error handling conventions.

---

## 4. Anticipated Technical Screening Questions

1. **Architecture & System Design:**
   * *"How would you architect a fault-tolerant ingestion pipeline handling unpredictable bursts in traffic?"*
   * *Focus:* Decoupled ingestion, message queues, rate limiting, and backpressure.

2. **Data & Storage Engineering:**
   * *"How do you diagnose query bottlenecks and design optimal composite indexes?"*
   * *Focus:* Execution plans, index selectivity, read-heavy versus write-heavy trade-offs.

3. **Service Communication & Contracts:**
   * *"What trade-offs do you consider between synchronous RPC/REST and event streaming?"*
   * *Focus:* Latency, coupling, eventual consistency, observability.

---

## 5. 48-Hour Technical Revision Roadmap

- **Day 1 (Morning):** System architecture patterns, caching mechanics, and concurrency fundamentals.
- **Day 1 (Afternoon):** Core programming language idiomatic practices, data structures, and algorithms.
- **Day 2 (Morning):** Distributed systems failure modes, transactions, and message queuing semantics.
- **Day 2 (Afternoon):** Mock system design whiteboard practice and reviewing past production debugging stories.
"""


def _build_behavioral_qa_template(
    company: str,
    role: str,
    job_description_text: str,
    candidate_story_bank: list[dict[str, Any]] | None = None,
) -> str:
    """Synthesize targeted behavioral screening questions mapped to candidate STAR anecdotes."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    star_sections: list[str] = []
    if candidate_story_bank:
        for idx, story in enumerate(candidate_story_bank, start=1):
            title = story.get("title", f"Project Experience {idx}")
            story_id = story.get("id", f"story-{idx}")
            competencies = story.get("competencies", [])
            comp_str = ", ".join(competencies) if isinstance(competencies, list) else str(competencies)
            situation = story.get("situation", "Described project context and business environment.")
            task = story.get("task", "Specific objective and constraints.")
            action = story.get("action", "Technical actions, decisions, and implementation.")
            result = story.get("result", "Measurable outcomes and business impact.")

            star_sections.append(
                f"""### Question {idx}: Alignment with Competency ({comp_str or "Technical Leadership"})
- **Anticipated Screening Prompt:** *"Tell me about a time you demonstrated leadership, ownership, or solved a complex technical bottleneck in a high-stakes environment."*
- **Mapped STAR Story:** **{title}** (`{story_id}`)
  - **Situation:** {situation}
  - **Task:** {task}
  - **Action:** {action}
  - **Result:** {result}
- **Delivery Strategy:** Deliver within 2 to 2.5 minutes; highlight specific technical decisions and quantitative impact."""
            )
    else:
        star_sections.append(
            """### Question 1: Complex Problem Solving & Technical Obstacles
- **Anticipated Screening Prompt:** *"Tell me about the most challenging technical bug or production bottleneck you resolved."*
- **Framework Recommendation:** STAR
  - **Situation:** Outline a high-throughput or complex system under latency / error pressure.
  - **Task:** Explain the specific performance or stability requirement you owned.
  - **Action:** Detail telemetry profiling, root-cause isolation, and architectural refactoring.
  - **Result:** Cite latency reduction percentage, uptime improvement, or developer hours saved.
- **Delivery Strategy:** Focus on structured diagnosis methodology rather than trial-and-error."""
        )
        star_sections.append(
            """### Question 2: Technical Disagreement & Engineering Collaboration
- **Anticipated Screening Prompt:** *"Describe a scenario where you disagreed with a colleague or stakeholder on technical approach."*
- **Framework Recommendation:** STAR
  - **Situation:** Context of competing architectural options or product requirements.
  - **Task:** Need to reach consensus without compromising timeline or engineering quality.
  - **Action:** Created data-driven POCs, benchmarked trade-offs, and engaged in blameless technical discussion.
  - **Result:** Delivered optimal architecture with shared team buy-in and zero friction."""
        )
        star_sections.append(
            """### Question 3: Ownership Under Ambiguity
- **Anticipated Screening Prompt:** *"Walk me through a project where requirements were ambiguous and you had to drive delivery."*
- **Framework Recommendation:** STAR
  - **Situation:** High-level product objective without granular engineering specifications.
  - **Task:** Scope the architecture, milestone delivery phases, and verification criteria.
  - **Action:** Interviewed stakeholders, wrote lightweight technical RFC, and executed phased rollout.
  - **Result:** Shipped MVP ahead of deadline; framework adopted as team standard."""
        )

    questions_body = "\n\n".join(star_sections)

    return f"""# Targeted Behavioral Interview Preparation

**Target Role:** {role}  
**Hiring Company:** {company}  
**Generated Date:** {date_str}

---

## 1. Behavioral Assessment Overview

Behavioral screening evaluates technical ownership, structured communication, cross-functional collaboration, and decision-making under real-world engineering constraints. This guide maps core competencies to concrete STAR (Situation, Task, Action, Result) narratives.

---

## 2. Predicted Questions & STAR Anecdote Mappings

{questions_body}

---

## 3. High-Impact Interview Delivery Guidelines

- **Keep It Concise:** Aim for 90 to 120 seconds per story, allocating 60% of speaking time to **Action**.
- **Own Your Contributions:** Frame achievements using active, first-person ownership ("I designed", "I benchmarked", "I refactored").
- **Quantify Results:** Conclude with metrics (latency reduction, traffic scale, error reduction, business value).
- **Emphasize Reflection:** Conclude stories with lessons learned or how the outcome improved team practices.
"""


def _build_company_briefing_template(
    company: str,
    role: str,
    job_description_text: str,
) -> str:
    """Synthesize strategic company briefing markdown document."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    detected_tech = _extract_tech_keywords_from_text(job_description_text)
    tech_str = ", ".join(t.capitalize() for t in detected_tech[:8]) if detected_tech else "Modern Cloud & Backend Services"

    return f"""# Company Briefing & Strategic Overview

**Target Company:** {company}  
**Target Role:** {role}  
**Analysis Date:** {date_str}

---

## 1. Company Mission & Value Proposition

**{company}** focuses on delivering high-reliability services, user-centric products, and scalable technology solutions. The organization prioritizes engineering excellence, high velocity, and robust system architecture.

### Strategic Priorities Reflected in Job Requirements
- Expanding scalable infrastructure to support customer growth and transaction volume.
- Maintaining high platform availability, security posture, and low latency.
- Fostering developer velocity and continuous delivery through automated tooling.

---

## 2. Engineering Culture & Technical Landscape

- **Primary Tech Stack Focus:** {tech_str}
- **Engineering Philosophy:**
  - **Pragmatic Architecture:** Balancing rapid feature delivery with maintainability and test coverage.
  - **End-to-End Ownership:** Engineers actively design, build, test, and monitor services in production.
  - **Collaborative Rigor:** Emphasis on peer code reviews, transparent post-mortems, and shared technical standards.

---

## 3. 30-60-90 Day High-Leverage Impact Plan

- **Days 1–30 (Onboard & Contextualize):**
  - Master the codebase architecture, CI/CD pipelines, and local development environments.
  - Deliver initial small PRs and shadow on-call rotations or team design sessions.
  - Establish working relationships with product managers, team peers, and stakeholders.

- **Days 31–60 (Contribute & Execute):**
  - Take independent ownership of core feature implementations or performance improvements.
  - Contribute to test automation, edge-case coverage, and documentation.
  - Participate actively in architecture design reviews and backlog grooming.

- **Days 61–90 (Lead & Scale):**
  - Propose optimizations to cut infrastructure bottlenecks or reduce technical debt.
  - Mentor new team members and share domain knowledge through internal tech talks or RFCs.
  - Help define technical milestones for subsequent roadmap quarters.

---

## 4. Strategic Reverse-Interview Questions

Prepare these thoughtful questions for your interviewers:

### Questions for the Hiring Manager:
1. *"What does an exceptional 90-day trajectory look like for this role, and what is the team's highest-priority technical challenge?"*
2. *"How does the engineering organization balance roadmap feature delivery against addressing architectural technical debt?"*

### Questions for Senior Peers / Technical Interviewers:
3. *"Could you walk me through a recent production incident and how the team approached diagnosis, blameless post-mortem, and permanent resolution?"*
4. *"How do engineers on this team evaluate and adopt new open-source tooling, libraries, or architectural paradigms?"*
"""


class DossierCreatorAgent:
    """Agent responsible for archiving job applications and synthesizing interview dossiers."""

    def __init__(self, history_root: Path | str | None = None) -> None:
        """Initialize the dossier creator agent.

        Args:
            history_root: Root directory for immutable application archives.
                Defaults to HISTORY_PATH environment variable or 'history'.
        """
        if history_root is None:
            raw_path = os.getenv("HISTORY_PATH", "history")
            self.history_root = Path(raw_path)
        else:
            self.history_root = Path(history_root)

    def archive_application(
        self,
        company: str,
        role: str,
        role_category: str = DEFAULT_ROLE_CATEGORY,
        portal_url: str = "",
        raw_job_description_md: str = "",
        submitted_form_qna: dict[str, Any] | list[dict[str, Any]] | None = None,
        tailored_resume_pdf_path: Path | str | None = None,
        tailored_resume_yaml_path: Path | str | None = None,
        gemini_client: Any | None = None,
        candidate_story_bank: list[dict[str, Any]] | None = None,
        candidate_skills: list[str] | None = None,
        generate_prep: bool = True,
        status: str = STATUS_SUBMITTED,
        date_str: str | None = None,
    ) -> Path:
        """Archive a completed job application and generate interview dossiers.

        Args:
            company: Company name.
            role: Job role title.
            role_category: 'full_time' or 'internships'.
            portal_url: URL of the job application portal.
            raw_job_description_md: Markdown text of the job description.
            submitted_form_qna: Form Q&A pairs or list of field records.
            tailored_resume_pdf_path: Optional path to compiled tailored resume PDF.
            tailored_resume_yaml_path: Optional path to tailored resume YAML.
            gemini_client: Optional GeminiClient instance for LLM synthesis.
            candidate_story_bank: Optional candidate STAR story bank.
            candidate_skills: Optional list of candidate technical skills.
            generate_prep: Whether to automatically generate interview prep dossiers.
            status: Submission status string (defaults to 'Submitted').
            date_str: Optional explicit YYYY-MM-DD date string for dossier slug.

        Returns:
            Path to the newly created dossier directory.
        """
        normalized_category = normalize_role_category(role_category)
        slug = sanitize_slug(company=company, role=role, date_str=date_str)

        dossier_dir = self.history_root / normalized_category / slug
        dossier_dir.mkdir(parents=True, exist_ok=True)

        # 1. Write application_meta.json
        meta_payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "company": company.strip(),
            "role": role.strip(),
            "portal_url": portal_url.strip(),
            "status": status.strip() or STATUS_SUBMITTED,
            "role_category": normalized_category,
            "slug": slug,
        }
        meta_file = dossier_dir / "application_meta.json"
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta_payload, f, indent=2, ensure_ascii=False)

        # 2. Write raw_job_description.md
        jd_file = dossier_dir / "raw_job_description.md"
        jd_content = raw_job_description_md.strip()
        if not jd_content:
            jd_content = f"# {role} at {company}\n\n*No job description text provided.*"
        with open(jd_file, "w", encoding="utf-8") as f:
            f.write(jd_content)

        # 3. Write submitted_form_qna.json
        qna_file = dossier_dir / "submitted_form_qna.json"
        qna_payload = submitted_form_qna if submitted_form_qna is not None else {}
        with open(qna_file, "w", encoding="utf-8") as f:
            json.dump(qna_payload, f, indent=2, ensure_ascii=False)

        # 4. Copy tailored resume PDF if provided and exists
        if tailored_resume_pdf_path is not None:
            pdf_path = Path(tailored_resume_pdf_path)
            if pdf_path.is_file():
                dest_pdf = dossier_dir / "tailored_resume.pdf"
                shutil.copy2(pdf_path, dest_pdf)

        # 5. Copy tailored resume YAML if provided and exists
        if tailored_resume_yaml_path is not None:
            yaml_path = Path(tailored_resume_yaml_path)
            if yaml_path.is_file():
                dest_yaml = dossier_dir / "tailored_resume.yaml"
                shutil.copy2(yaml_path, dest_yaml)

        # 6. Generate interview preparation materials
        if generate_prep:
            self.generate_interview_prep(
                dossier_dir=dossier_dir,
                job_description_text=jd_content,
                candidate_story_bank=candidate_story_bank,
                gemini_client=gemini_client,
                company=company,
                role=role,
                candidate_skills=candidate_skills,
            )

        logger.info("Successfully created application dossier at %s", dossier_dir)
        return dossier_dir

    def generate_interview_prep(
        self,
        dossier_dir: Path | str,
        job_description_text: str,
        candidate_story_bank: list[dict[str, Any]] | None = None,
        gemini_client: Any | None = None,
        company: str = "",
        role: str = "",
        candidate_skills: list[str] | None = None,
    ) -> Path:
        """Synthesize 3 tailored markdown preparation dossiers inside interview_prep subfolder.

        Generates:
            1. technical_skills_gap.md
            2. targeted_behavioral_qa.md
            3. company_briefing.md

        Args:
            dossier_dir: Target dossier directory path.
            job_description_text: Job posting text or raw markdown.
            candidate_story_bank: Optional candidate STAR story bank.
            gemini_client: Optional GeminiClient or compatible LLM client.
            company: Hiring organization name (inferred from meta if empty).
            role: Target job title (inferred from meta if empty).
            candidate_skills: Optional candidate skills list for gap analysis.

        Returns:
            Path to the interview_prep subfolder.
        """
        dossier_path = Path(dossier_dir)
        prep_dir = dossier_path / "interview_prep"
        prep_dir.mkdir(parents=True, exist_ok=True)

        # Infer company and role from metadata if omitted
        if not company or not role:
            meta_path = dossier_path / "application_meta.json"
            if meta_path.is_file():
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                        company = company or meta.get("company", "Target Company")
                        role = role or meta.get("role", "Target Role")
                except Exception as exc:
                    logger.debug("Could not read application_meta.json: %s", exc)

        company = company or "Target Company"
        role = role or "Target Role"

        # 1. Technical Skills Gap Analysis
        skills_gap_path = prep_dir / "technical_skills_gap.md"
        prompt_skills = (
            f"Analyze the following job description against candidate background for {company} - {role}.\n"
            "Generate a comprehensive, structured technical skills gap analysis document in Markdown.\n\n"
            f"JOB DESCRIPTION:\n{job_description_text.strip()}\n\n"
            f"CANDIDATE KNOWN SKILLS:\n{', '.join(candidate_skills) if candidate_skills else 'Standard full-stack / backend profile'}"
        )
        sys_skills = "You are an elite technical interview preparation specialist. Synthesize accurate, actionable markdown."
        skills_content = _generate_markdown_with_gemini(gemini_client, prompt_skills, sys_skills)
        if not skills_content:
            skills_content = _build_skills_gap_template(
                company=company,
                role=role,
                job_description_text=job_description_text,
                candidate_skills=candidate_skills,
            )
        with open(skills_gap_path, "w", encoding="utf-8") as f:
            f.write(skills_content)

        # 2. Targeted Behavioral Q&A
        behavioral_path = prep_dir / "targeted_behavioral_qa.md"
        prompt_behavioral = (
            f"Generate targeted behavioral interview preparation in Markdown for {company} - {role}.\n"
            "Predict key behavioral screening prompts and map them to the candidate's STAR story bank.\n\n"
            f"JOB DESCRIPTION:\n{job_description_text.strip()}\n\n"
            f"STORY BANK:\n{json.dumps(candidate_story_bank or [], indent=2)}"
        )
        sys_behavioral = "You are an executive career coach. Map job requirements to rigorous STAR stories in Markdown."
        behavioral_content = _generate_markdown_with_gemini(gemini_client, prompt_behavioral, sys_behavioral)
        if not behavioral_content:
            behavioral_content = _build_behavioral_qa_template(
                company=company,
                role=role,
                job_description_text=job_description_text,
                candidate_story_bank=candidate_story_bank,
            )
        with open(behavioral_path, "w", encoding="utf-8") as f:
            f.write(behavioral_content)

        # 3. Company Briefing
        briefing_path = prep_dir / "company_briefing.md"
        prompt_briefing = (
            f"Synthesize an insightful company briefing and strategic interview overview in Markdown for {company} - {role}.\n\n"
            f"JOB DESCRIPTION:\n{job_description_text.strip()}"
        )
        sys_briefing = "You are a tech industry analyst. Produce structured, highly strategic company dossiers in Markdown."
        briefing_content = _generate_markdown_with_gemini(gemini_client, prompt_briefing, sys_briefing)
        if not briefing_content:
            briefing_content = _build_company_briefing_template(
                company=company,
                role=role,
                job_description_text=job_description_text,
            )
        with open(briefing_path, "w", encoding="utf-8") as f:
            f.write(briefing_content)

        return prep_dir


def generate_interview_prep(
    dossier_dir: Path | str,
    job_description_text: str,
    candidate_story_bank: list[dict[str, Any]] | None = None,
    gemini_client: Any | None = None,
    company: str = "",
    role: str = "",
    candidate_skills: list[str] | None = None,
) -> Path:
    """Convenience functional interface for generating interview prep dossiers."""
    agent = DossierCreatorAgent()
    return agent.generate_interview_prep(
        dossier_dir=dossier_dir,
        job_description_text=job_description_text,
        candidate_story_bank=candidate_story_bank,
        gemini_client=gemini_client,
        company=company,
        role=role,
        candidate_skills=candidate_skills,
    )


__all__ = [
    "DEFAULT_ROLE_CATEGORY",
    "STATUS_SUBMITTED",
    "SUPPORTED_ROLE_CATEGORIES",
    "DossierCreatorAgent",
    "generate_interview_prep",
    "normalize_role_category",
    "sanitize_slug",
]
