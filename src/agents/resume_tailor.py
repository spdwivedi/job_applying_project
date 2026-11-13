"""Resume Tailor Agent for JobApplier-Core.

Tailors candidate RenderCV profiles to match target job requirements
and compiles ATS-compliant PDFs via RenderCV and Typst.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from src.engine.llm import GeminiClient
from src.engine.resume_compiler import compile_resume

logger = logging.getLogger(__name__)

# Fallback Pydantic schema if peer module is still being initialized.
try:
    from src.agents.job_profiler import JobProfileSchema
except ImportError:
    class JobProfileSchema(BaseModel):  # type: ignore[no-redef]
        """Structured representation of target job requirements."""

        job_title: str = ""
        company_name: str = ""
        location: str = ""
        work_type: str = "remote"
        role_category: str = "full_time"
        required_skills: list[str] = Field(default_factory=list)
        preferred_skills: list[str] = Field(default_factory=list)
        tech_stack: list[str] = Field(default_factory=list)
        extracted_screening_questions: list[str] = Field(default_factory=list)
        raw_markdown: str = ""


class TailoredSummarySchema(BaseModel):
    """Schema for Gemini-assisted summary tailoring."""

    tailored_summary: str = Field(
        ...,
        description="One to two concise sentences aligning candidate background with target role.",
    )


class ResumeTailorAgent:
    """Agent that tailors RenderCV YAML profiles and compiles ATS-compliant PDFs."""

    def __init__(
        self,
        master_profile_path: Path | str | None = None,
    ) -> None:
        """Initialize the ResumeTailorAgent.

        Args:
            master_profile_path: Optional explicit path to master profile YAML.
                When omitted, defaults to Path(os.getenv("VAULT_PATH", "vault")) / "master_profile.yaml".
        """
        if master_profile_path is None:
            vault_dir = Path(os.getenv("VAULT_PATH", "vault"))
            self.master_profile_path = vault_dir / "master_profile.yaml"
        else:
            self.master_profile_path = Path(master_profile_path)

        self._profile_data: dict[str, Any] = {}
        self.cv: dict[str, Any] = {}
        self.design: dict[str, Any] = {}

        self.load_profile()

    def load_profile(self) -> None:
        """Load candidate profile details and design configuration from YAML file."""
        target_path = self.master_profile_path
        if not target_path.is_file():
            example_candidate = target_path.with_name(target_path.name + ".example")
            if example_candidate.is_file():
                target_path = example_candidate

        if target_path.is_file():
            try:
                content = target_path.read_text(encoding="utf-8")
                self._profile_data = yaml.safe_load(content) or {}
            except Exception:
                logger.exception("Failed to parse YAML master profile from '%s'.", target_path)
                self._profile_data = {}
        else:
            logger.warning("Master profile file not found at '%s'.", target_path)
            self._profile_data = {}

        self.cv = self._profile_data.get("cv", {})
        self.design = self._profile_data.get(
            "design",
            {"theme": "classic", "page": {"size": "us-letter"}},
        )

    def _build_skill_weights(self, job_profile: JobProfileSchema) -> dict[str, float]:
        """Build normalized skill importance weight map from job requirements."""
        weights: dict[str, float] = {}

        for skill in job_profile.required_skills:
            clean = skill.strip().lower()
            if clean:
                weights[clean] = 3.0

        for skill in job_profile.preferred_skills:
            clean = skill.strip().lower()
            if clean and clean not in weights:
                weights[clean] = 2.0

        for skill in job_profile.tech_stack:
            clean = skill.strip().lower()
            if clean and clean not in weights:
                weights[clean] = 2.0

        stop_words = {"senior", "junior", "lead", "staff", "principal", "software", "engineer", "developer", "intern"}
        if job_profile.job_title:
            title_tokens = re.findall(r"\w+", job_profile.job_title.lower())
            for token in title_tokens:
                if token not in stop_words and len(token) > 2 and token not in weights:
                    weights[token] = 1.5

        return weights

    def _score_text(self, text: str, skill_weights: dict[str, float]) -> float:
        """Score a text highlight or item by keyword relevance and quantified impact."""
        if not text:
            return 0.0

        text_lower = text.lower()
        score = 0.0

        for skill, weight in skill_weights.items():
            pattern = r"(?<!\w)" + re.escape(skill) + r"(?!\w)"
            if re.search(pattern, text_lower):
                score += weight

        if re.search(r"\b\d+([.,]\d+)?(%|x|k|m|\+)?\b", text_lower):
            score += 0.5

        return score

    def _tailor_experience(
        self,
        experience_list: list[dict[str, Any]],
        skill_weights: dict[str, float],
    ) -> list[dict[str, Any]]:
        """Reorder experience highlights so the most job-relevant bullets appear first."""
        tailored: list[dict[str, Any]] = []

        for exp in experience_list:
            exp_copy = copy.deepcopy(exp)
            highlights = exp_copy.get("highlights", [])
            if isinstance(highlights, list) and len(highlights) > 1:
                # Stable sort preserving original order for ties, highest score first
                exp_copy["highlights"] = sorted(
                    highlights,
                    key=lambda h: self._score_text(str(h), skill_weights),
                    reverse=True,
                )
            tailored.append(exp_copy)

        return tailored

    def _tailor_skills(
        self,
        skills_list: list[dict[str, Any]],
        skill_weights: dict[str, float],
    ) -> list[dict[str, Any]]:
        """Prioritize job-relevant skills within each skill category."""
        tailored: list[dict[str, Any]] = []

        for skill_entry in skills_list:
            entry_copy = copy.deepcopy(skill_entry)
            details = entry_copy.get("details", "")
            if isinstance(details, str) and "," in details:
                tokens = [t.strip() for t in details.split(",") if t.strip()]
                sorted_tokens = sorted(
                    tokens,
                    key=lambda token: self._score_text(token, skill_weights),
                    reverse=True,
                )
                entry_copy["details"] = ", ".join(sorted_tokens)
            tailored.append(entry_copy)

        return tailored

    def _tailor_projects(
        self,
        projects_list: list[dict[str, Any]],
        skill_weights: dict[str, float],
    ) -> list[dict[str, Any]]:
        """Reorder project highlights and sort projects by relevance."""
        tailored: list[dict[str, Any]] = []

        for proj in projects_list:
            proj_copy = copy.deepcopy(proj)
            highlights = proj_copy.get("highlights", [])
            if isinstance(highlights, list) and len(highlights) > 1:
                proj_copy["highlights"] = sorted(
                    highlights,
                    key=lambda h: self._score_text(str(h), skill_weights),
                    reverse=True,
                )
            tailored.append(proj_copy)

        if len(tailored) > 1:
            def project_score(p: dict[str, Any]) -> float:
                h_text = " ".join(str(h) for h in p.get("highlights", []))
                return self._score_text(p.get("name", "") + " " + h_text, skill_weights)

            tailored = sorted(tailored, key=project_score, reverse=True)

        return tailored

    def _generate_tailored_summary_gemini(
        self,
        gemini_client: GeminiClient,
        job_profile: JobProfileSchema,
        original_summary: str,
        skill_weights: dict[str, float],
    ) -> str | None:
        """Use Gemini to draft an ATS-aligned summary strictly grounded in candidate background."""
        try:
            top_skills = list(skill_weights.keys())[:5]
            system_instruction = (
                "You are an expert resume strategist. Refine the candidate's existing summary "
                "into 1-2 powerful, truthful sentences highlighting their alignment with the target role. "
                "CRITICAL: Never invent achievements, years of experience, or unverified skills."
            )
            prompt = (
                f"TARGET ROLE: {job_profile.job_title} at {job_profile.company_name}\n"
                f"KEY REQUIRED SKILLS: {', '.join(top_skills)}\n"
                f"ORIGINAL SUMMARY: {original_summary}\n\n"
                "Refine into 1-2 strictly grounded sentences."
            )

            result = gemini_client.generate_structured(
                prompt=prompt,
                response_schema=TailoredSummarySchema,
                system_instruction=system_instruction,
                temperature=0.1,
            )
            if result and result.tailored_summary and result.tailored_summary.strip():
                return result.tailored_summary.strip()
        except Exception as exc:
            logger.warning("Gemini summary refinement failed (%s); keeping original summary.", exc)
        return None

    def tailor_resume(
        self,
        job_profile: JobProfileSchema,
        output_pdf_path: Path | str,
        output_yaml_path: Path | str | None = None,
        gemini_client: GeminiClient | None = None,
    ) -> bool:
        """Tailor candidate resume to target job profile and compile an ATS-compliant PDF.

        Args:
            job_profile: Target job profile containing required skills and role metadata.
            output_pdf_path: Destination path for the compiled ATS PDF resume.
            output_yaml_path: Optional destination path to persist the tailored RenderCV YAML.
            gemini_client: Optional GeminiClient for AI-assisted summary optimization.

        Returns:
            True if compilation succeeds and produced a valid non-empty PDF; False otherwise.
        """
        output_pdf = Path(output_pdf_path).resolve()
        output_yaml = Path(output_yaml_path).resolve() if output_yaml_path is not None else None

        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        if output_yaml is not None:
            output_yaml.parent.mkdir(parents=True, exist_ok=True)

        cv_tailored = copy.deepcopy(self.cv)
        design_tailored = copy.deepcopy(self.design)

        skill_weights = self._build_skill_weights(job_profile)
        sections = cv_tailored.get("sections", {})

        # 1. Tailor experience highlights
        if "experience" in sections and isinstance(sections["experience"], list):
            sections["experience"] = self._tailor_experience(sections["experience"], skill_weights)

        # 2. Tailor skills ordering
        if "skills" in sections and isinstance(sections["skills"], list):
            sections["skills"] = self._tailor_skills(sections["skills"], skill_weights)

        # 3. Tailor projects ordering
        if "projects" in sections and isinstance(sections["projects"], list):
            sections["projects"] = self._tailor_projects(sections["projects"], skill_weights)

        # 4. Optional Gemini summary refinement
        if gemini_client is not None and "summary" in sections:
            summary_list = sections.get("summary", [])
            original_summary = summary_list[0] if summary_list else ""
            if original_summary:
                refined = self._generate_tailored_summary_gemini(
                    gemini_client,
                    job_profile,
                    original_summary,
                    skill_weights,
                )
                if refined:
                    sections["summary"] = [refined]

        cv_tailored["sections"] = sections
        rendercv_payload: dict[str, Any] = {
            "cv": cv_tailored,
            "design": design_tailored,
        }

        # 5. Write RenderCV YAML and invoke compiler
        try:
            if output_yaml is not None:
                output_yaml.write_text(
                    yaml.dump(rendercv_payload, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
                yaml_to_compile = output_yaml
                compile_success = compile_resume(yaml_to_compile, output_pdf)
            else:
                with tempfile.TemporaryDirectory(prefix="tailored_cv_") as tmp_dir:
                    tmp_yaml = Path(tmp_dir) / "tailored_resume.yaml"
                    tmp_yaml.write_text(
                        yaml.dump(rendercv_payload, sort_keys=False, allow_unicode=True),
                        encoding="utf-8",
                    )
                    compile_success = compile_resume(tmp_yaml, output_pdf)

            is_valid = bool(compile_success and output_pdf.is_file() and output_pdf.stat().st_size > 0)
            if not is_valid:
                logger.error("Resume compilation did not yield a valid PDF at '%s'.", output_pdf)
            return is_valid

        except Exception:
            logger.exception("Failed during resume tailoring and compilation for '%s'.", output_pdf)
            return False


__all__ = [
    "ResumeTailorAgent",
    "TailoredSummarySchema",
]
