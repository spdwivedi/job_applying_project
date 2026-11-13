"""Answer Crafter Agent for JobApplier-Core.

Synthesizes truthful, profile-grounded answers for application form fields
using candidate profile data, work authorization details, and STAR story bank.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from src.engine.llm import GeminiClient

logger = logging.getLogger(__name__)

# Fallback Pydantic schemas if peer modules are still being initialized.
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

try:
    from src.engine.form_parser import FormField, FormSchema
except ImportError:
    class FormField(BaseModel):  # type: ignore[no-redef]
        """Representation of an interactive application form control."""

        id: str = ""
        name: str = ""
        label: str = ""
        type: str = "text"
        required: bool = False
        options: list[str] = Field(default_factory=list)
        value: str = ""

    class FormSchema(BaseModel):  # type: ignore[no-redef]
        """Collection of parsed form fields for a job application page."""

        fields: list[FormField] = Field(default_factory=list)

        def get_field_by_id(self, field_id: str) -> FormField | None:
            for field in self.fields:
                if field.id == field_id:
                    return field
            return None

        def get_resume_upload_field(self) -> FormField | None:
            for field in self.fields:
                combined = f"{field.name} {field.label} {field.id}".lower()
                if field.type == "file" and any(k in combined for k in ("resume", "cv", "attachment")):
                    return field
            return None

        def to_dict(self) -> dict[str, Any]:
            return self.model_dump()


class OpenEndedAnswerSchema(BaseModel):
    """Structured response schema for Gemini-assisted open-ended question answering."""

    reasoning: str = Field(
        ...,
        description="Brief grounding rationale referencing verifiable candidate profile facts.",
    )
    answer: str = Field(
        ...,
        description="Concise, truthful, and professional answer tailored to the question.",
    )


class AnswerCrafterAgent:
    """Agent that crafts truthful, candidate-grounded answers for application form fields."""

    def __init__(
        self,
        master_profile_path: Path | str | None = None,
    ) -> None:
        """Initialize the AnswerCrafterAgent.

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
        self.work_authorization: dict[str, Any] = {}
        self.story_bank: list[dict[str, Any]] = []

        self.load_profile()

    def load_profile(self) -> None:
        """Load candidate profile details from YAML file."""
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
        self.work_authorization = self._profile_data.get("work_authorization", {})
        self.story_bank = self._profile_data.get("story_bank", [])

    def _get_social_handle(self, network_name: str) -> str:
        """Retrieve social network username from candidate profile."""
        target = network_name.lower()
        for item in self.cv.get("social_networks", []):
            if isinstance(item, dict) and item.get("network", "").lower() == target:
                return str(item.get("username", "")).strip()
        return ""

    def _match_best_option(self, raw_answer: str, options: list[str]) -> str:
        """Match a proposed raw answer against a list of valid form options."""
        if not options:
            return raw_answer

        for opt in options:
            if opt.strip().lower() == raw_answer.strip().lower():
                return opt

        norm_answer = raw_answer.strip().lower()

        if norm_answer in ("no", "false", "none", "0"):
            for opt in options:
                opt_lower = opt.lower()
                if "no" in opt_lower and "yes" not in opt_lower:
                    return opt
                if "not require" in opt_lower or "will not" in opt_lower:
                    return opt

        if norm_answer in ("yes", "true", "1"):
            for opt in options:
                opt_lower = opt.lower()
                if "yes" in opt_lower and "no" not in opt_lower:
                    return opt
                if "authorized" in opt_lower or "eligible" in opt_lower:
                    return opt

        if any(term in norm_answer for term in ("decline", "prefer not", "wish not")):
            for opt in options:
                opt_lower = opt.lower()
                if any(k in opt_lower for k in ("decline", "prefer not", "do not wish", "choose not")):
                    return opt

        for opt in options:
            if norm_answer in opt.lower() or opt.lower() in norm_answer:
                return opt

        return options[0] if options else raw_answer

    def _answer_personal_contact(self, field: FormField, combined: str) -> str | None:
        """Map standard personal and contact fields directly from cv profile attributes."""
        if any(k in combined for k in ("first_name", "firstname", "first name", "given name", "forename")):
            full_name = self.cv.get("name", "")
            return full_name.split()[0] if full_name else ""

        if any(k in combined for k in ("last_name", "lastname", "last name", "family name", "surname")):
            full_name = self.cv.get("name", "")
            parts = full_name.split()
            return parts[-1] if len(parts) > 1 else ""

        if any(k in combined for k in ("full_name", "fullname", "legal name", "candidate name")):
            return self.cv.get("name", "")

        if re.search(r"\bname\b", combined) and not any(k in combined for k in ("first", "last", "user", "file", "company", "school")):
            return self.cv.get("name", "")

        if any(k in combined for k in ("email", "e-mail")):
            return self.cv.get("email", "")

        if any(k in combined for k in ("phone", "mobile", "telephone", "tel", "cell")):
            return self.cv.get("phone", "")

        if any(k in combined for k in ("linkedin", "linked in")):
            username = self._get_social_handle("LinkedIn")
            if not username:
                return ""
            if username.startswith("http://") or username.startswith("https://"):
                return username
            return f"https://www.linkedin.com/in/{username}"

        if any(k in combined for k in ("github", "git hub")):
            username = self._get_social_handle("GitHub")
            if not username:
                return ""
            if username.startswith("http://") or username.startswith("https://"):
                return username
            return f"https://github.com/{username}"

        if any(k in combined for k in ("portfolio", "personal website", "website", "personal site", "homepage", "blog")):
            return self.cv.get("website", "")

        if any(k in combined for k in ("city", "town")):
            location = self.cv.get("location", "")
            return location.split(",")[0].strip() if "," in location else location

        if any(k in combined for k in ("state", "province", "region")):
            location = self.cv.get("location", "")
            return location.split(",")[1].strip() if "," in location else ""

        if any(k in combined for k in ("zip", "postal", "zipcode", "postal code")):
            return ""

        if any(k in combined for k in ("country", "nation")):
            countries = self.work_authorization.get("countries_authorized", [])
            return countries[0] if countries else "United States"

        if re.search(r"\b(location|address)\b", combined) and not any(k in combined for k in ("email", "ip", "mac")):
            return self.cv.get("location", "")

        return None

    def _answer_work_authorization(self, field: FormField, combined: str) -> str | None:
        """Map employment authorization, visa sponsorship, and availability questions."""
        if any(k in combined for k in (
            "authorized to work",
            "work authorization",
            "legally authorized",
            "eligible to work",
            "legal right to work",
            "work permit",
            "right to work",
        )):
            return "Yes"

        if any(k in combined for k in (
            "sponsorship",
            "require sponsorship",
            "visa sponsorship",
            "future sponsorship",
            "require visa",
            "h-1b",
            "immigration",
        )):
            sponsorship_req = bool(self.work_authorization.get("sponsorship_required", False))
            future_req = bool(self.work_authorization.get("future_sponsorship_required", False))
            return "Yes" if (sponsorship_req or future_req) else "No"

        if any(k in combined for k in ("security clearance", "clearance level")):
            return str(self.work_authorization.get("security_clearance", "None"))

        if any(k in combined for k in ("relocate", "relocation", "willing to relocate")):
            prefs = self.work_authorization.get("relocation_preferences", {})
            willing = bool(prefs.get("willing_to_relocate", True))
            return "Yes" if willing else "No"

        if any(k in combined for k in ("notice period", "how soon can you start", "earliest start", "availability")):
            weeks = self.work_authorization.get("notice_period_weeks", 2)
            try:
                weeks_int = int(weeks)
                return f"{weeks_int} weeks" if weeks_int > 0 else "Immediately"
            except (ValueError, TypeError):
                return str(weeks)

        if any(k in combined for k in ("work mode", "work arrangement", "remote preference")):
            prefs = self.work_authorization.get("work_mode_preferences", ["Hybrid", "Remote", "On-site"])
            return prefs[0] if prefs else "Hybrid"

        return None

    def _answer_eeo(self, field: FormField, combined: str) -> str | None:
        """Provide safe default answers for voluntary Equal Opportunity / demographic questions."""
        if any(k in combined for k in (
            "gender",
            "veteran",
            "disability",
            "race",
            "ethnicity",
            "hispanic",
            "latino",
            "sexual orientation",
        )):
            return "Decline to self-identify"
        return None

    def _select_relevant_story(self, query: str) -> dict[str, Any] | None:
        """Select the most relevant candidate STAR story based on query keywords."""
        if not self.story_bank:
            return None

        query_words = set(re.findall(r"\w+", query.lower()))
        best_story = self.story_bank[0]
        max_overlap = -1

        for story in self.story_bank:
            story_text = " ".join([
                story.get("title", ""),
                " ".join(story.get("competencies", [])),
                story.get("situation", ""),
                story.get("action", ""),
                story.get("result", ""),
            ]).lower()
            story_words = set(re.findall(r"\w+", story_text))
            overlap = len(query_words & story_words)
            if overlap > max_overlap:
                max_overlap = overlap
                best_story = story

        return best_story

    def _synthesize_grounded_answer_offline(
        self,
        field: FormField,
        combined: str,
        job_profile: JobProfileSchema | None,
    ) -> str:
        """Synthesize a truthful, grounded response from candidate profile and STAR stories without an LLM."""
        company = job_profile.company_name if job_profile and job_profile.company_name else "your organization"
        job_title = job_profile.job_title if job_profile and job_profile.job_title else "this role"

        summary_bullets = self.cv.get("sections", {}).get("summary", [])
        summary_text = summary_bullets[0] if summary_bullets else ""

        is_why_question = any(k in combined for k in ("why", "interest", "excited", "drawn to", "attracted"))
        if is_why_question:
            if summary_text:
                return (
                    f"I am excited to apply for the {job_title} role at {company}. "
                    f"{summary_text} I am eager to contribute my hands-on technical experience "
                    f"and deliver high-impact results for your team."
                )
            return (
                f"I am enthusiastic about the {job_title} role at {company} because it closely "
                f"aligns with my engineering background and passion for building scalable, reliable systems."
            )

        story = self._select_relevant_story(combined)
        if story:
            situation = story.get("situation", "").strip()
            action = story.get("action", "").strip()
            result = story.get("result", "").strip()
            title = story.get("title", "").strip()

            parts: list[str] = []
            if title:
                parts.append(f"In my work on {title}:")
            if situation:
                parts.append(situation)
            if action:
                parts.append(action)
            if result:
                parts.append(f"As a result, {result}")

            return " ".join(parts)

        experiences = self.cv.get("sections", {}).get("experience", [])
        if experiences:
            exp = experiences[0]
            company_prev = exp.get("company", "")
            position_prev = exp.get("position", "")
            highlights = exp.get("highlights", [])
            highlight_text = f" Specifically, {highlights[0]}" if highlights else ""
            return f"As a {position_prev} at {company_prev}, I developed key technical initiatives.{highlight_text}"

        return summary_text or "I bring hands-on experience and a strong commitment to engineering excellence."

    def _answer_open_ended(
        self,
        field: FormField,
        combined: str,
        job_profile: JobProfileSchema | None,
        gemini_client: GeminiClient | None,
    ) -> str:
        """Answer open-ended questions using Gemini with candidate STAR stories or fallback synthesis."""
        if gemini_client is not None:
            try:
                question_text = field.label or field.name or combined
                job_desc = ""
                if job_profile is not None:
                    job_desc = (
                        f"Company: {job_profile.company_name}\n"
                        f"Job Title: {job_profile.job_title}\n"
                        f"Required Skills: {', '.join(job_profile.required_skills)}\n"
                        f"Preferred Skills: {', '.join(job_profile.preferred_skills)}"
                    )

                stories_desc = []
                for s in self.story_bank:
                    stories_desc.append(
                        f"- Title: {s.get('title')}\n"
                        f"  Competencies: {', '.join(s.get('competencies', []))}\n"
                        f"  Situation: {s.get('situation')}\n"
                        f"  Action: {s.get('action')}\n"
                        f"  Result: {s.get('result')}"
                    )
                stories_context = "\n\n".join(stories_desc)

                cv_summary = " ".join(self.cv.get("sections", {}).get("summary", []))

                system_instruction = (
                    "You are an expert career agent crafting concise, highly articulate, and strictly truthful "
                    "answers for a job application form on behalf of a candidate. "
                    "CRITICAL RULES:\n"
                    "1. Ground all details entirely in the candidate's real profile and STAR stories.\n"
                    "2. Never fabricate credentials, companies, projects, or metrics.\n"
                    "3. Directly address the specific question.\n"
                    "4. Keep the answer professional and concise (1-2 paragraphs)."
                )

                prompt = (
                    f"APPLICATION QUESTION:\n{question_text}\n\n"
                    f"TARGET JOB CONTEXT:\n{job_desc}\n\n"
                    f"CANDIDATE STAR STORIES:\n{stories_context}\n\n"
                    f"CANDIDATE SUMMARY:\n{cv_summary}\n\n"
                    "Draft a truthful, grounded response to this question."
                )

                parsed = gemini_client.generate_structured(
                    prompt=prompt,
                    response_schema=OpenEndedAnswerSchema,
                    system_instruction=system_instruction,
                    temperature=0.2,
                )
                if parsed and parsed.answer and parsed.answer.strip():
                    return parsed.answer.strip()
            except Exception as exc:
                logger.warning("Gemini answer generation failed (%s); using offline grounded synthesis.", exc)

        return self._synthesize_grounded_answer_offline(field, combined, job_profile)

    def craft_answers(
        self,
        form_schema: FormSchema,
        job_profile: JobProfileSchema | None = None,
        gemini_client: GeminiClient | None = None,
    ) -> dict[str, str]:
        """Craft answers for all fields in a job application form schema.

        Args:
            form_schema: FormSchema containing detected form fields.
            job_profile: Optional target JobProfileSchema for role alignment.
            gemini_client: Optional GeminiClient for drafting open-ended responses.

        Returns:
            Dictionary mapping field identifier (id/name) to crafted answer string.
        """
        answers: dict[str, str] = {}

        for idx, field in enumerate(form_schema.fields):
            combined = f"{field.id} {field.name} {field.label}".lower()

            # Ignore file upload inputs in text answer mapping
            if field.type == "file" or any(k in combined for k in ("resume", "curriculum vitae", "upload cv", "attachment")):
                continue

            raw_answer: str | None = None

            # 1. Personal & contact fields
            raw_answer = self._answer_personal_contact(field, combined)

            # 2. Work authorization fields
            if raw_answer is None:
                raw_answer = self._answer_work_authorization(field, combined)

            # 3. Equal Opportunity / demographic fields
            if raw_answer is None:
                raw_answer = self._answer_eeo(field, combined)

            # 4. Open-ended / behavioral / screening questions
            if raw_answer is None:
                is_open_ended = (
                    field.type in ("textarea", "text")
                    or len(field.label) > 30
                    or any(k in combined for k in ("why", "describe", "tell us", "project", "challenge", "cover letter", "experience with"))
                )
                if is_open_ended:
                    raw_answer = self._answer_open_ended(field, combined, job_profile, gemini_client)
                else:
                    raw_answer = ""

            # Ensure answer conforms to field options when specified
            final_answer = self._match_best_option(raw_answer or "", field.options)

            # Record answer keyed by id and name
            primary_key = field.id or field.name or field.label or f"field_{idx}"
            answers[primary_key] = final_answer

            if field.id and field.id != primary_key:
                answers[field.id] = final_answer
            if field.name and field.name != primary_key and field.name != field.id:
                answers[field.name] = final_answer

            field.value = final_answer

        return answers


__all__ = [
    "AnswerCrafterAgent",
    "OpenEndedAnswerSchema",
]
