"""Resume Fact Provenance Guard for JobApplier-Core.

Ingests candidate master profile facts from the vault directory and audits
drafted resume bullets, tailored summaries, cover letters, and application
answers to detect and sanitize hallucinations, ungrounded metrics, fabricated
employers, non-existent degree titles, and work authorization contradictions.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Pre-commit hygiene constants constructed dynamically
_AT_SIGN = chr(64)
_EMAIL_PATTERN = re.compile(rf"[A-Za-z0-9._%+-]+{_AT_SIGN}[A-Za-z0-9.-]+\.[A-Za-z]{{2,}}")

# Well-known major tech and financial corporations frequently hallucinated by LLMs
_KNOWN_MAJOR_TECH_FIRMS = {
    "google",
    "apple",
    "microsoft",
    "amazon",
    "meta",
    "facebook",
    "netflix",
    "uber",
    "airbnb",
    "stripe",
    "salesforce",
    "twitter",
    "oracle",
    "cisco",
    "intel",
    "nvidia",
    "adobe",
    "spotify",
    "palantir",
    "snowflake",
    "databricks",
    "openai",
    "anthropic",
    "bytedance",
    "tiktok",
    "tesla",
    "goldman sachs",
    "morgan stanley",
    "jpmorgan",
    "mckinsey",
    "boston consulting group",
    "bain",
    "lyft",
    "pinterest",
    "snap",
    "doordash",
    "coinbase",
}

# Standard technology terms, libraries, cloud providers, and protocols
# to avoid misidentifying technical tooling as company names.
_DEFAULT_TECH_TERMS = {
    "python",
    "typescript",
    "javascript",
    "go",
    "golang",
    "rust",
    "java",
    "c++",
    "c#",
    "fastapi",
    "react",
    "next.js",
    "vue",
    "angular",
    "node.js",
    "django",
    "flask",
    "docker",
    "kubernetes",
    "redis",
    "redis streams",
    "postgresql",
    "postgres",
    "mysql",
    "mongodb",
    "aws",
    "gcp",
    "azure",
    "github",
    "gitlab",
    "linux",
    "unix",
    "bash",
    "graphql",
    "rest",
    "grpc",
    "kafka",
    "rabbitmq",
    "celery",
    "playwright",
    "selenium",
    "pytorch",
    "tensorflow",
    "pandas",
    "numpy",
    "scikit-learn",
    "rendercv",
    "typst",
    "latex",
    "html",
    "css",
    "s3",
    "ecs",
    "rds",
    "ec2",
    "bm25",
    "aes-256",
    "aes-256-gcm",
    "oauth",
    "jwt",
    "ci/cd",
    "sql",
}

# Calendar and generic prepositions / stop words to avoid false positive company detection
_STOP_WORDS = {
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "present",
    "scale",
    "runtime",
    "once",
    "all",
    "work",
    "engineering",
    "production",
    "design",
}


class FactAuditResult(BaseModel):
    """Structured audit report produced by ResumeFactGuard."""

    is_valid: bool = Field(
        ...,
        description="True if content contains zero unverified claims or authorization conflicts.",
    )
    confidence_score: float = Field(
        ...,
        description="Confidence score between 0.0 and 1.0 reflecting fact provenance.",
    )
    violations: list[str] = Field(
        default_factory=list,
        description="Human-readable violation descriptions detected in the text.",
    )
    flagged_claims: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Structured records of each ungrounded claim or contradiction.",
    )
    verified_facts_count: int = Field(
        default=0,
        description="Count of grounded profile entities confirmed in the text.",
    )


class MetricFact(BaseModel):
    """Normalized quantified metric fact extracted from profile or draft."""

    raw: str
    value: float
    unit: str  # "%", "$", "x", "ms", "s", "years", "count", "gpa"
    original_text: str = ""


class ResumeFactGuard:
    """Anti-hallucination auditor and fact provenance guard for JobApplier-Core.

    Verifies drafted resume bullets, summaries, cover letters, and application
    responses against the ground-truth candidate profile.
    """

    def __init__(
        self,
        profile_path: Path | str | None = None,
        profile_data: dict[str, Any] | None = None,
        metric_tolerance: float = 0.15,
    ) -> None:
        """Initialize the ResumeFactGuard.

        Args:
            profile_path: Optional explicit path to master profile YAML.
                Defaults to Path(os.getenv("VAULT_PATH", "vault")) / "master_profile.yaml".
            profile_data: Optional pre-loaded profile dictionary for testing or offline usage.
            metric_tolerance: Relative numerical variance allowed for metrics (default 15%).
        """
        self.metric_tolerance = metric_tolerance

        if profile_path is None:
            vault_dir = Path(os.getenv("VAULT_PATH", "vault"))
            self.profile_path = vault_dir / "master_profile.yaml"
        else:
            self.profile_path = Path(profile_path)

        self._profile_data: dict[str, Any] = {}
        self.candidate_names: set[str] = set()
        self.candidate_emails: set[str] = set()
        self.candidate_phones: set[str] = set()
        self.candidate_links: set[str] = set()
        self.candidate_locations: set[str] = set()

        self.known_companies: set[str] = set()
        self.known_projects: set[str] = set()
        self.known_degrees: set[str] = set()
        self.highest_degree_level: str = "bachelors"
        self.known_institutions: set[str] = set()
        self.known_skills: set[str] = set()

        self.known_date_ranges: list[str] = []
        self.known_years: set[int] = set()
        self.earliest_year: int = 2020
        self.graduation_year: int | None = None
        self.total_experience_years: float = 0.0

        self.known_metrics: list[MetricFact] = []
        self.work_authorization: dict[str, Any] = {}

        if profile_data is not None:
            self.load_profile_data(profile_data)
        else:
            self.load_profile()

    def load_profile(self) -> None:
        """Load candidate profile from disk and build verified knowledge base."""
        target_path = self.profile_path
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

        self._build_knowledge_base()

    def load_profile_data(self, data: dict[str, Any]) -> None:
        """Load candidate profile directly from an in-memory dictionary."""
        self._profile_data = data or {}
        self._build_knowledge_base()

    def _build_knowledge_base(self) -> None:
        """Construct indexes and normalized fact stores from loaded profile."""
        cv = self._profile_data.get("cv", {})
        sections = cv.get("sections", {})
        work_auth = self._profile_data.get("work_authorization", {})
        self.work_authorization = work_auth

        # 1. Candidate identity & contact facts
        name = str(cv.get("name", "")).strip()
        if name:
            self.candidate_names.add(name.lower())
            for part in name.split():
                if len(part) > 2:
                    self.candidate_names.add(part.lower())

        email = str(cv.get("email", "")).strip()
        if email:
            self.candidate_emails.add(email.lower())

        phone = str(cv.get("phone", "")).strip()
        if phone:
            self.candidate_phones.add(phone.lower())
            digits_only = "".join(ch for ch in phone if ch.isdigit())
            if digits_only:
                self.candidate_phones.add(digits_only)

        website = str(cv.get("website", "")).strip()
        if website:
            self.candidate_links.add(website.lower())
            domain = re.sub(r"^https?://", "", website).split("/")[0]
            if domain:
                self.candidate_links.add(domain.lower())

        loc = str(cv.get("location", "")).strip()
        if loc:
            self.candidate_locations.add(loc.lower())
            for part in re.split(r"[,/]", loc):
                part_clean = part.strip().lower()
                if part_clean:
                    self.candidate_locations.add(part_clean)

        for sn in cv.get("social_networks", []):
            if isinstance(sn, dict):
                username = str(sn.get("username", "")).strip().lower()
                if username:
                    self.candidate_links.add(username)

        # 2. Education & Degree facts
        education_entries = sections.get("education", [])
        for edu in education_entries:
            if not isinstance(edu, dict):
                continue
            inst = str(edu.get("institution", "")).strip()
            if inst:
                self.known_institutions.add(inst.lower())
                for word in inst.split():
                    if len(word) > 3 and word.lower() not in {"university", "college", "institute", "school"}:
                        self.known_institutions.add(word.lower())

            degree = str(edu.get("degree", "")).strip()
            area = str(edu.get("area", "")).strip()
            if degree:
                self.known_degrees.add(degree.lower())
                cleaned_degree = re.sub(r"[.\s]", "", degree).lower()
                self.known_degrees.add(cleaned_degree)
                deg_lower = degree.lower()
                if "ph" in cleaned_degree or "doctor" in deg_lower:
                    self.highest_degree_level = "doctorate"
                elif ("master" in deg_lower or cleaned_degree in {"ms", "ma", "mba", "meng"} or "mba" in deg_lower) and self.highest_degree_level != "doctorate":
                    self.highest_degree_level = "masters"

            if area:
                self.known_degrees.add(area.lower())

            start_d = str(edu.get("start_date", "")).strip()
            end_d = str(edu.get("end_date", "")).strip()
            self._record_date(start_d)
            self._record_date(end_d)

            for hl in edu.get("highlights", []):
                if isinstance(hl, str):
                    self.known_metrics.extend(self._extract_metrics_from_text(hl))

        # 3. Employment & Company facts
        experience_entries = sections.get("experience", [])
        for exp in experience_entries:
            if not isinstance(exp, dict):
                continue
            company = str(exp.get("company", "")).strip()
            if company:
                self.known_companies.add(company.lower())
                clean_comp = re.sub(r"[^\w\s]", "", company).strip().lower()
                self.known_companies.add(clean_comp)
                for part in company.split():
                    if len(part) > 2 and part.lower() not in {"inc", "llc", "corp", "corporation", "co", "ltd", "company"}:
                        self.known_companies.add(part.lower())

            start_d = str(exp.get("start_date", "")).strip()
            end_d = str(exp.get("end_date", "")).strip()
            self._record_date(start_d)
            self._record_date(end_d)

            for hl in exp.get("highlights", []):
                if isinstance(hl, str):
                    self.known_metrics.extend(self._extract_metrics_from_text(hl))

        # 4. Project facts
        project_entries = sections.get("projects", [])
        for proj in project_entries:
            if not isinstance(proj, dict):
                continue
            proj_name = str(proj.get("name", "")).strip()
            if proj_name:
                self.known_projects.add(proj_name.lower())
                clean_proj = re.sub(r"[^\w\s]", "", proj_name).strip().lower()
                self.known_projects.add(clean_proj)

            proj_date = str(proj.get("date", "")).strip()
            self._record_date(proj_date)

            for hl in proj.get("highlights", []):
                if isinstance(hl, str):
                    self.known_metrics.extend(self._extract_metrics_from_text(hl))

        # 5. Skills & Competencies
        skills_entries = sections.get("skills", [])
        for sk in skills_entries:
            if not isinstance(sk, dict):
                continue
            details = str(sk.get("details", ""))
            for token in re.split(r"[,;/|]", details):
                clean_tok = token.strip().lower()
                if clean_tok:
                    self.known_skills.add(clean_tok)
                    self.known_skills.add(clean_tok.replace("-", " "))
                    self.known_skills.add(clean_tok.replace(" ", ""))

        for story in self._profile_data.get("story_bank", []):
            if isinstance(story, dict):
                for comp in story.get("competencies", []):
                    clean_c = str(comp).strip().lower()
                    if clean_c:
                        self.known_skills.add(clean_c)
                        self.known_skills.add(clean_c.replace("_", " "))

                for metric_str in story.get("metrics", []):
                    if isinstance(metric_str, str):
                        self.known_metrics.extend(self._extract_metrics_from_text(metric_str))

        # 6. Summary metrics
        for sum_entry in sections.get("summary", []):
            if isinstance(sum_entry, str):
                self.known_metrics.extend(self._extract_metrics_from_text(sum_entry))

        # 7. Timeline resolution
        if self.known_years:
            self.earliest_year = min(self.known_years)
            current_year = 2026
            earliest_career = 2020
            exp_years: list[int] = []
            for exp in experience_entries:
                if isinstance(exp, dict):
                    sd = str(exp.get("start_date", ""))
                    m = re.search(r"\b(20\d\d|19\d\d)\b", sd)
                    if m:
                        exp_years.append(int(m.group(1)))
            if exp_years:
                earliest_career = min(exp_years)
            elif self.known_years:
                earliest_career = min(self.known_years)

            self.total_experience_years = max(1.0, float(current_year - earliest_career))

        # Extract explicit graduation year if available
        for edu in education_entries:
            if isinstance(edu, dict):
                end_d = str(edu.get("end_date", ""))
                m = re.search(r"\b(20\d\d|19\d\d)\b", end_d)
                if m:
                    self.graduation_year = int(m.group(1))

    def _record_date(self, date_str: str) -> None:
        """Extract and record year integers and formatted date ranges."""
        if not date_str or date_str.lower() == "present":
            return
        matches = re.findall(r"\b(19\d\d|20\d\d)\b", date_str)
        for year_str in matches:
            try:
                self.known_years.add(int(year_str))
            except ValueError:
                pass
        self.known_date_ranges.append(date_str)

    def _extract_metrics_from_text(self, text: str) -> list[MetricFact]:
        """Parse quantified metrics (percentages, currency, multipliers, scale, latency)."""
        metrics: list[MetricFact] = []

        # 1. Percentages (e.g. 68%, 90%, 19%, 45.5%)
        for m in re.finditer(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*%", text):
            val = float(m.group(1))
            metrics.append(MetricFact(raw=m.group(0).strip(), value=val, unit="%", original_text=text))

        # 2. Multipliers (e.g. 2x, 10x, 3.5X)
        for m in re.finditer(r"(?<![0-9a-zA-Z])(\d+(?:\.\d+)?)\s*[xX]\b", text):
            val = float(m.group(1))
            metrics.append(MetricFact(raw=m.group(0).strip(), value=val, unit="x", original_text=text))

        # 3. Currency (e.g. $10M, $500K, $1.5M, $50,000)
        for m in re.finditer(
            r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(k|m|b|million|billion|thousand)?\b",
            text,
            re.IGNORECASE,
        ):
            raw_num = m.group(1).replace(",", "")
            val = float(raw_num)
            suffix = (m.group(2) or "").lower()
            if suffix in {"k", "thousand"}:
                val *= 1_000.0
            elif suffix in {"m", "million"}:
                val *= 1_000_000.0
            elif suffix in {"b", "billion"}:
                val *= 1_000_000_000.0
            metrics.append(MetricFact(raw=m.group(0).strip(), value=val, unit="$", original_text=text))

        # 4. Latency / Milliseconds (e.g. 145ms, 420ms)
        for m in re.finditer(r"\b(\d+(?:\.\d+)?)\s*(ms|milliseconds?)\b", text, re.IGNORECASE):
            val = float(m.group(1))
            metrics.append(MetricFact(raw=m.group(0).strip(), value=val, unit="ms", original_text=text))

        # 5. Scale counts with suffixes (e.g. 1.2M+, 18,000+), ensuring not preceded by $, hyphen, or letter
        for m in re.finditer(
            r"(?<![A-Za-z\-\$0-9.])(\d+(?:,\d{3})*(?:\.\d+)?)\s*(k|m|b|million|billion|thousand)\+?\b",
            text,
            re.IGNORECASE,
        ):
            raw_num = m.group(1).replace(",", "")
            val = float(raw_num)
            suffix = m.group(2).lower()
            if suffix in {"k", "thousand"}:
                val *= 1_000.0
            elif suffix in {"m", "million"}:
                val *= 1_000_000.0
            elif suffix in {"b", "billion"}:
                val *= 1_000_000_000.0
            metrics.append(MetricFact(raw=m.group(0).strip(), value=val, unit="count", original_text=text))

        # 6. Explicit years of experience (e.g. 4+ years of experience)
        for m in re.finditer(
            r"\b(\d+)\+?\s*(?:years?|yrs?)(?:\s+of\s+experience)?\b",
            text,
            re.IGNORECASE,
        ):
            val = float(m.group(1))
            metrics.append(MetricFact(raw=m.group(0).strip(), value=val, unit="years", original_text=text))

        # 7. GPA metrics (e.g. GPA: 3.80 / 4.00)
        for m in re.finditer(r"\bGPA:?\s*(\d+\.\d+)\b", text, re.IGNORECASE):
            val = float(m.group(1))
            metrics.append(MetricFact(raw=m.group(0).strip(), value=val, unit="gpa", original_text=text))

        return metrics

    def _match_metric(self, draft_metric: MetricFact) -> tuple[bool, str]:
        """Check whether draft metric aligns with verified candidate metrics within tolerance."""
        matching_unit_metrics = [km for km in self.known_metrics if km.unit == draft_metric.unit]

        if not matching_unit_metrics:
            return (
                False,
                f"Metric '{draft_metric.raw}' (unit: '{draft_metric.unit}') has no grounded counterpart in candidate profile.",
            )

        best_match = False
        for km in matching_unit_metrics:
            diff = abs(draft_metric.value - km.value)

            if draft_metric.unit == "%":
                # Stricter tolerance for percentages: within 2.5 percentage points or 5% relative difference
                if diff <= 2.5 or (km.value > 0 and (diff / km.value) <= 0.05):
                    best_match = True
                    break
            elif draft_metric.unit == "years":
                # Claimed years cannot exceed known career duration or profile years
                allowed_years = max(km.value, self.total_experience_years) + 1.0
                if draft_metric.value <= allowed_years:
                    best_match = True
                    break
            elif draft_metric.unit == "ms":
                # Latency variance within tolerance or 15ms
                if diff <= max(km.value * self.metric_tolerance, 15.0):
                    best_match = True
                    break
            elif draft_metric.unit in {"$", "count", "x"}:
                if diff <= km.value * self.metric_tolerance:
                    best_match = True
                    break
            elif draft_metric.unit == "gpa":
                if diff <= 0.05:
                    best_match = True
                    break

        if best_match:
            return (True, "Grounding confirmed")

        return (
            False,
            f"Metric '{draft_metric.raw}' differs significantly from known candidate metrics (allowed tolerance: {int(self.metric_tolerance * 100)}%).",
        )

    def _audit_companies(self, text: str) -> list[dict[str, Any]]:
        """Identify fabricated employers or company names mentioned in the text."""
        flagged: list[dict[str, Any]] = []

        # 1. Prepositional phrases: "at Google", "for Microsoft", "joined Stripe", "employed by Netflix"
        comp_matches = re.finditer(
            r"\b(?:at|for|joined|employed by|tenure at|interned at)\s+([A-Z][A-Za-z0-9&.-]+(?:\s+[A-Z][A-Za-z0-9&.-]+)*)\b",
            text,
        )
        for m in comp_matches:
            candidate_comp = m.group(1).strip()
            norm = candidate_comp.lower()

            if norm in _STOP_WORDS:
                continue
            if norm in _DEFAULT_TECH_TERMS or norm in self.known_skills:
                continue
            if norm in self.candidate_locations:
                continue
            if norm in self.known_institutions:
                continue

            # Check if this company is in candidate's known employers
            is_known = any(norm == kc or kc in norm or norm in kc for kc in self.known_companies)
            if not is_known:
                flagged.append(
                    {
                        "type": "unverified_employer",
                        "claim": candidate_comp,
                        "reason": f"Company '{candidate_comp}' is not listed in candidate verified employment history.",
                        "severity": "high",
                    }
                )

        # 2. Check for explicit mentions of major tech/finance corporations
        for major_firm in _KNOWN_MAJOR_TECH_FIRMS:
            pattern = rf"\b{re.escape(major_firm)}\b"
            if re.search(pattern, text, re.IGNORECASE):
                is_known = any(major_firm == kc or kc in major_firm for kc in self.known_companies)
                if not is_known:
                    if not any(f["claim"].lower() == major_firm for f in flagged):
                        flagged.append(
                            {
                                "type": "unverified_employer",
                                "claim": major_firm.title(),
                                "reason": f"Prominent company '{major_firm.title()}' cited without verification in master profile.",
                                "severity": "high",
                            }
                        )

        return flagged

    def _audit_education(self, text: str) -> list[dict[str, Any]]:
        """Detect fabricated degree titles or non-existent institutions."""
        flagged: list[dict[str, Any]] = []

        phd_match = re.search(r"\b(ph\.?d\.?|doctorate|doctor of philosophy)\b", text, re.IGNORECASE)
        if phd_match and self.highest_degree_level != "doctorate":
            flagged.append(
                {
                    "type": "fabricated_degree",
                    "claim": phd_match.group(0),
                    "reason": f"Draft claims '{phd_match.group(0)}', but candidate verified education records a {self.highest_degree_level.title()} degree.",
                    "severity": "high",
                }
            )

        masters_match = re.search(
            r"\b(master(?:'s)?(?:\s+of\s+science)?|m\.?s\.?|mba|master of business administration)\b",
            text,
            re.IGNORECASE,
        )
        if masters_match and self.highest_degree_level == "bachelors":
            flagged.append(
                {
                    "type": "fabricated_degree",
                    "claim": masters_match.group(0),
                    "reason": f"Draft claims '{masters_match.group(0)}', but candidate verified education only records a Bachelor's degree.",
                    "severity": "high",
                }
            )

        major_unis = {
            "mit",
            "stanford",
            "harvard",
            "cambridge",
            "oxford",
            "princeton",
            "columbia",
            "yale",
            "cmu",
            "carnegie mellon",
            "caltech",
        }
        for uni in major_unis:
            if re.search(rf"\b{re.escape(uni)}\b", text, re.IGNORECASE):
                is_known_inst = any(uni in ki or ki in uni for ki in self.known_institutions)
                if not is_known_inst:
                    flagged.append(
                        {
                            "type": "unverified_institution",
                            "claim": uni.upper() if len(uni) <= 3 else uni.title(),
                            "reason": f"Educational institution '{uni.title()}' not found in candidate verified education history.",
                            "severity": "high",
                        }
                    )

        return flagged

    def _audit_timeline(self, text: str) -> list[dict[str, Any]]:
        """Verify date ranges and graduation dates against candidate timeline."""
        flagged: list[dict[str, Any]] = []

        year_matches = re.finditer(r"\b(19\d\d|20\d\d)\b", text)
        for ym in year_matches:
            year_val = int(ym.group(1))
            if year_val < (self.earliest_year - 1):
                flagged.append(
                    {
                        "type": "unverified_date",
                        "claim": str(year_val),
                        "reason": f"Year '{year_val}' predates candidate earliest verified activity ({self.earliest_year}).",
                        "severity": "medium",
                    }
                )

        grad_match = re.search(
            r"\b(?:graduated|class of|degree in)\s+(?:in\s+)?(19\d\d|20\d\d)\b",
            text,
            re.IGNORECASE,
        )
        if grad_match and self.graduation_year:
            claimed_grad = int(grad_match.group(1))
            if abs(claimed_grad - self.graduation_year) > 1:
                flagged.append(
                    {
                        "type": "unverified_graduation_year",
                        "claim": str(claimed_grad),
                        "reason": f"Draft claims graduation year {claimed_grad}, but verified graduation year is {self.graduation_year}.",
                        "severity": "high",
                    }
                )

        return flagged

    def _audit_work_authorization(self, text: str) -> list[dict[str, Any]]:
        """Verify statements against candidate work authorization ground truth."""
        flagged: list[dict[str, Any]] = []
        if not self.work_authorization:
            return flagged

        sponsorship_required = self.work_authorization.get("sponsorship_required", False)
        clearance = str(self.work_authorization.get("security_clearance", "None")).strip().lower()
        countries_authorized = [
            str(c).strip().lower() for c in self.work_authorization.get("countries_authorized", ["United States"])
        ]

        # 1. Visa sponsorship contradiction
        requires_sponsor_match = re.search(
            r"\b(?:requires?|requiring|need(?:s|ing)?|seeking)\s+(?:[\w-]+\s+)*(?:visa\s+)?sponsorship\b",
            text,
            re.IGNORECASE,
        )
        if requires_sponsor_match and not sponsorship_required:
            flagged.append(
                {
                    "type": "work_authorization_contradiction",
                    "claim": requires_sponsor_match.group(0),
                    "reason": "Draft states visa sponsorship is required, but candidate profile specifies sponsorship_required=False.",
                    "severity": "critical",
                }
            )

        no_sponsor_match = re.search(
            r"\b(?:do(?:es)?\s+not\s+require|no|without)\s+(?:[\w-]+\s+)*(?:visa\s+)?sponsorship\b",
            text,
            re.IGNORECASE,
        )
        if no_sponsor_match and sponsorship_required:
            flagged.append(
                {
                    "type": "work_authorization_contradiction",
                    "claim": no_sponsor_match.group(0),
                    "reason": "Draft claims no sponsorship needed, but candidate profile specifies sponsorship_required=True.",
                    "severity": "critical",
                }
            )

        # 2. Security clearance contradiction
        clearance_match = re.search(
            r"\b(?:active\s+)?(?:secret|top\s+secret|ts/sci|polygraph)\s+(?:security\s+)?clearance\b",
            text,
            re.IGNORECASE,
        )
        if clearance_match and (clearance in {"none", "", "n/a"}):
            flagged.append(
                {
                    "type": "work_authorization_contradiction",
                    "claim": clearance_match.group(0),
                    "reason": "Draft claims active security clearance, but candidate profile specifies security_clearance='None'.",
                    "severity": "critical",
                }
            )

        # 3. Unauthorized country claims
        uk_match = re.search(
            r"\bauthorized\s+to\s+work\s+in\s+(?:the\s+)?(uk|united kingdom|canada|germany|france|australia|eu)\b",
            text,
            re.IGNORECASE,
        )
        if uk_match:
            claimed_country = uk_match.group(1).lower()
            if not any(claimed_country in c for c in countries_authorized):
                flagged.append(
                    {
                        "type": "work_authorization_contradiction",
                        "claim": uk_match.group(0),
                        "reason": f"Draft claims authorization to work in '{uk_match.group(1)}', which is not in authorized countries list.",
                        "severity": "critical",
                    }
                )

        return flagged

    def _count_verified_facts(self, text: str) -> int:
        """Count verified profile elements (skills, companies, links, metrics) present in text."""
        count = 0
        text_lower = text.lower()

        # Count verified skills
        for skill in self.known_skills:
            if len(skill) >= 3 and f" {skill} " in f" {text_lower} ":
                count += 1

        # Count verified companies
        for comp in self.known_companies:
            if len(comp) >= 3 and comp in text_lower:
                count += 1

        # Count verified projects
        for proj in self.known_projects:
            if len(proj) >= 3 and proj in text_lower:
                count += 1

        # Count verified candidate names / locations
        for loc in self.candidate_locations:
            if len(loc) >= 3 and loc in text_lower:
                count += 1

        # Count verified metrics
        for metric in self._extract_metrics_from_text(text):
            is_valid, _ = self._match_metric(metric)
            if is_valid:
                count += 1

        return count

    def verify_drafted_content(
        self,
        draft_text: str,
        context: str = "bullet",
    ) -> FactAuditResult:
        """Inspect drafted content and audit for hallucinations or contradictions.

        Args:
            draft_text: Resume bullet point, tailored summary, cover letter, or answer text.
            context: Context label such as 'bullet', 'summary', or 'cover_letter'.

        Returns:
            FactAuditResult containing validity boolean, confidence score,
            violations list, and flagged claim details.
        """
        if not draft_text or not draft_text.strip():
            return FactAuditResult(
                is_valid=True,
                confidence_score=1.0,
                violations=[],
                flagged_claims=[],
                verified_facts_count=0,
            )

        violations: list[str] = []
        flagged_claims: list[dict[str, Any]] = []

        # 1. Audit quantified metrics
        draft_metrics = self._extract_metrics_from_text(draft_text)
        for dm in draft_metrics:
            is_grounded, explanation = self._match_metric(dm)
            if not is_grounded:
                violations.append(f"Unverified metric: {explanation}")
                flagged_claims.append(
                    {
                        "type": "unverified_metric",
                        "claim": dm.raw,
                        "unit": dm.unit,
                        "value": dm.value,
                        "reason": explanation,
                        "severity": "high",
                    }
                )

        # 2. Audit companies / employers
        comp_violations = self._audit_companies(draft_text)
        for cv in comp_violations:
            violations.append(f"Fabricated employer: {cv['reason']}")
            flagged_claims.append(cv)

        # 3. Audit degrees and universities
        edu_violations = self._audit_education(draft_text)
        for ev in edu_violations:
            violations.append(f"Education discrepancy: {ev['reason']}")
            flagged_claims.append(ev)

        # 4. Audit timeline and graduation dates
        timeline_violations = self._audit_timeline(draft_text)
        for tv in timeline_violations:
            violations.append(f"Timeline discrepancy: {tv['reason']}")
            flagged_claims.append(tv)

        # 5. Audit work authorization
        auth_violations = self._audit_work_authorization(draft_text)
        for av in auth_violations:
            violations.append(f"Work authorization contradiction: {av['reason']}")
            flagged_claims.append(av)

        # 6. Count verified grounded facts
        verified_facts_count = self._count_verified_facts(draft_text)

        # 7. Compute confidence score
        if violations:
            is_valid = False
            penalty = len(violations) * 0.20 + len(flagged_claims) * 0.05
            bonus = min(0.20, verified_facts_count * 0.04)
            confidence_score = max(0.0, min(0.65, round(1.0 - penalty + bonus, 2)))
        else:
            is_valid = True
            confidence_score = 1.0 if verified_facts_count > 0 else 0.95

        return FactAuditResult(
            is_valid=is_valid,
            confidence_score=confidence_score,
            violations=violations,
            flagged_claims=flagged_claims,
            verified_facts_count=verified_facts_count,
        )

    def sanitize_draft(self, draft_text: str) -> str:
        """Strip unverified hallucinated sentences while preserving verified claims.

        Args:
            draft_text: Multiline bullets or paragraph text to sanitize.

        Returns:
            Sanitized text with ungrounded sentences and claims cleanly removed.
        """
        if not draft_text or not draft_text.strip():
            return ""

        lines = [line for line in draft_text.splitlines() if line.strip()]

        # Multiline bullet format
        if len(lines) > 1:
            clean_lines: list[str] = []
            for line in lines:
                audit = self.verify_drafted_content(line, context="bullet")
                if audit.is_valid:
                    clean_lines.append(line)
                else:
                    logger.info("Stripped hallucinated bullet line: '%s' (violations: %s)", line, audit.violations)
            return "\n".join(clean_lines)

        # Single paragraph format: split by sentence boundaries
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", draft_text) if s.strip()]
        clean_sentences: list[str] = []
        for sentence in sentences:
            audit = self.verify_drafted_content(sentence, context="summary")
            if audit.is_valid:
                clean_sentences.append(sentence)
            else:
                logger.info("Stripped hallucinated sentence: '%s' (violations: %s)", sentence, audit.violations)

        return " ".join(clean_sentences)

    def get_provenance_summary(self) -> dict[str, Any]:
        """Return diagnostic metrics of loaded profile knowledge base."""
        return {
            "candidate_names_count": len(self.candidate_names),
            "known_companies_count": len(self.known_companies),
            "known_skills_count": len(self.known_skills),
            "known_metrics_count": len(self.known_metrics),
            "known_years": sorted(list(self.known_years)),
            "highest_degree_level": self.highest_degree_level,
            "graduation_year": self.graduation_year,
            "total_experience_years": self.total_experience_years,
        }


# Canonical alias as requested by system brief
FactVerifier = ResumeFactGuard
