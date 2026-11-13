"""Browser Automation Driver (Safe Form Filler) for JobApplier-Core.

Provides safe, automated form filling across job application portals
with strict human-in-the-loop (HITL) safety enforcement. Automated
submission clicks are intercepted and prevented, requiring explicit
human approval via a valid security token.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    from playwright.sync_api import Locator, Page
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    Locator = Any  # type: ignore
    Page = Any  # type: ignore

try:
    from src.engine.form_parser import FormField, FormSchema
except (ImportError, ModuleNotFoundError):
    from pydantic import BaseModel, Field

    class FormField(BaseModel):
        id: str
        name: str = ""
        label: str = ""
        type: str = "text"
        required: bool = False
        options: list[str] = Field(default_factory=list)
        value: str = ""

    class FormSchema(BaseModel):
        fields: list[FormField] = Field(default_factory=list)

        def get_field_by_id(self, field_id: str) -> FormField | None:
            for field in self.fields:
                if field.id == field_id:
                    return field
            return None

        def get_resume_upload_field(self) -> FormField | None:
            for field in self.fields:
                if (field.type or "").lower() == "file":
                    return field
                identifiers = f"{field.id} {field.name} {field.label}".lower()
                if any(kw in identifiers for kw in ["resume", "cv", "attachment", "document"]):
                    return field
            return None

        def to_dict(self) -> dict[str, Any]:
            if hasattr(self, "model_dump"):
                return self.model_dump()
            return self.dict()

logger = logging.getLogger(__name__)

# State constants
STATUS_IDLE = "STATUS_IDLE"
STATUS_FILLING = "STATUS_FILLING"
STATUS_AWAITING_HUMAN_APPROVAL = "STATUS_AWAITING_HUMAN_APPROVAL"
STATUS_SUBMITTED = "STATUS_SUBMITTED"
STATUS_ERROR = "STATUS_ERROR"

# Submission safety regex pattern matching portal submit/apply buttons
SUBMISSION_INTERCEPT_PATTERN = re.compile(
    r"\b(submit|apply|send\s+application|complete\s+application|submit\s+application)\b",
    re.IGNORECASE,
)


class BrowserDriverAgent:
    """Automated browser driver for filling job application forms with strict HITL safety guards."""

    STATUS_IDLE = STATUS_IDLE
    STATUS_FILLING = STATUS_FILLING
    STATUS_AWAITING_HUMAN_APPROVAL = STATUS_AWAITING_HUMAN_APPROVAL
    STATUS_SUBMITTED = STATUS_SUBMITTED
    STATUS_ERROR = STATUS_ERROR

    def __init__(self) -> None:
        self.status: str = STATUS_IDLE
        self.filled_fields: list[str] = []
        self.uploaded_files: list[str] = []
        self.blocked_actions: list[str] = []
        self.approval_token: Optional[str] = None
        self.last_error: Optional[str] = None

    def reset(self) -> None:
        """Reset internal state to idle."""
        self.status = STATUS_IDLE
        self.filled_fields = []
        self.uploaded_files = []
        self.blocked_actions = []
        self.approval_token = None
        self.last_error = None

    def is_submission_control(self, locator: Locator) -> bool:
        """Inspect a DOM control to determine if it is a submission button/action."""
        try:
            control_type = (locator.get_attribute("type") or "").strip().lower()
            if control_type == "submit":
                return True

            text = (locator.text_content() or "").strip()
            value = (locator.get_attribute("value") or "").strip()
            aria = (locator.get_attribute("aria-label") or "").strip()
            name = (locator.get_attribute("name") or "").strip()
            elem_id = (locator.get_attribute("id") or "").strip()

            combined = f"{text} {value} {aria} {name} {elem_id}".lower()
            if SUBMISSION_INTERCEPT_PATTERN.search(combined):
                return True
        except Exception as exc:
            logger.debug("Error checking submission control: %s", exc)

        return False

    def safe_click(self, locator: Locator) -> bool:
        """Safely click an element, strictly blocking any click if it is a submission control."""
        if self.is_submission_control(locator):
            msg = "Blocked automated click on submission control. Explicit human approval is required."
            logger.warning(msg)
            self.blocked_actions.append(msg)
            self.status = STATUS_AWAITING_HUMAN_APPROVAL
            return False

        locator.click()
        return True

    def _find_answer_for_field(
        self, field: FormField, answers: dict[str, Any]
    ) -> Optional[str]:
        """Find the corresponding answer for a form field using exact and normalized key matching."""
        if not answers:
            return None

        # 1. Exact match on id
        if field.id and field.id in answers:
            return str(answers[field.id])

        # 2. Exact match on name
        if field.name and field.name in answers:
            return str(answers[field.name])

        # 3. Exact match on label
        if field.label and field.label in answers:
            return str(answers[field.label])

        # 4. Normalized match (lowercase, alphanumeric normalization)
        def normalize_key(k: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(k).strip().lower())

        norm_answers = {normalize_key(k): str(v) for k, v in answers.items()}

        for candidate in [field.id, field.name, field.label]:
            if candidate:
                norm_c = normalize_key(candidate)
                if norm_c and norm_c in norm_answers:
                    return norm_answers[norm_c]

        # 5. Substring / fuzzy heuristic
        for candidate in [field.label, field.name, field.id]:
            if candidate:
                norm_c = normalize_key(candidate)
                if not norm_c:
                    continue
                for k_norm, val in norm_answers.items():
                    if k_norm and (norm_c in k_norm or k_norm in norm_c):
                        return val

        return None

    def _locate_element_for_field(self, page: Page, field: FormField) -> Optional[Locator]:
        """Locate the DOM element corresponding to a FormField."""
        # 1. By ID if present
        if field.id:
            try:
                loc = page.locator(f"id={field.id}")
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass
            try:
                loc = page.locator(f'[id="{field.id}"]')
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass

        # 2. By Name attribute
        if field.name:
            try:
                loc = page.locator(f'[name="{field.name}"]')
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass

        # 3. By Label text
        if field.label:
            try:
                loc = page.get_by_label(field.label, exact=False)
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass

        # 4. By placeholder text
        if field.label:
            try:
                loc = page.get_by_placeholder(field.label, exact=False)
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass

        # 5. By aria-label
        for key in [field.label, field.name, field.id]:
            if key:
                try:
                    loc = page.locator(f'[aria-label*="{key}" i]')
                    if loc.count() > 0:
                        return loc.first
                except Exception:
                    pass

        return None

    def _fill_field(
        self,
        page: Page,
        field: FormField,
        answer_value: str,
        resume_pdf_path: Optional[Union[Path, str]] = None,
    ) -> bool:
        """Safely fill an individual form field based on its type."""
        field_type = (field.type or "text").strip().lower()

        # Safety: do not interact with or click submit controls during filling
        if field_type in ("submit", "button"):
            self.blocked_actions.append(
                f"Skipped control '{field.id or field.name or field.label}' with type '{field_type}'"
            )
            return False

        # 1. File Upload Field
        if field_type == "file":
            target_path: Optional[Path] = None
            if resume_pdf_path:
                p = Path(resume_pdf_path).resolve()
                if p.exists():
                    target_path = p
            elif answer_value:
                p = Path(answer_value).resolve()
                if p.exists():
                    target_path = p

            if target_path:
                locator = self._locate_element_for_field(page, field)
                if locator and locator.count() > 0:
                    locator.set_input_files(str(target_path))
                    self.uploaded_files.append(str(target_path))
                    self.filled_fields.append(field.id or field.name or field.label)
                    return True
            return False

        # 2. Radio Button Field
        if field_type == "radio":
            if field.name:
                loc = page.locator(
                    f'input[type="radio"][name="{field.name}"][value="{answer_value}"]'
                )
                if loc.count() > 0:
                    loc.first.check()
                    self.filled_fields.append(field.id or field.name or field.label)
                    return True

                radios = page.locator(f'input[type="radio"][name="{field.name}"]').all()
                for r in radios:
                    val = (r.get_attribute("value") or "").strip()
                    if val.lower() == answer_value.lower():
                        r.check()
                        self.filled_fields.append(field.id or field.name or field.label)
                        return True

            try:
                loc = page.get_by_label(answer_value, exact=False)
                if loc.count() > 0:
                    loc.first.check()
                    self.filled_fields.append(field.id or field.name or field.label)
                    return True
            except Exception:
                pass

            locator = self._locate_element_for_field(page, field)
            if locator and locator.count() > 0:
                locator.check()
                self.filled_fields.append(field.id or field.name or field.label)
                return True
            return False

        # 3. Checkbox Field
        if field_type == "checkbox":
            locator = self._locate_element_for_field(page, field)
            if locator and locator.count() > 0:
                truthy = answer_value.strip().lower() in (
                    "true",
                    "1",
                    "yes",
                    "on",
                    "checked",
                    "agree",
                )
                falsy = answer_value.strip().lower() in ("false", "0", "no", "off", "unchecked")
                if truthy:
                    locator.check()
                elif falsy:
                    locator.uncheck()
                else:
                    locator.check()
                self.filled_fields.append(field.id or field.name or field.label)
                return True
            return False

        # 4. Select / Dropdown Field
        if field_type in ("select", "dropdown"):
            locator = self._locate_element_for_field(page, field)
            if locator and locator.count() > 0:
                try:
                    locator.select_option(label=answer_value)
                    self.filled_fields.append(field.id or field.name or field.label)
                    return True
                except Exception:
                    pass
                try:
                    locator.select_option(value=answer_value)
                    self.filled_fields.append(field.id or field.name or field.label)
                    return True
                except Exception:
                    pass
                try:
                    opts = locator.locator("option").all()
                    for opt in opts:
                        opt_val = (opt.get_attribute("value") or "").strip()
                        opt_text = (opt.text_content() or "").strip()
                        if answer_value.lower() in (opt_val.lower(), opt_text.lower()):
                            locator.select_option(value=opt_val or opt_text)
                            self.filled_fields.append(field.id or field.name or field.label)
                            return True
                except Exception:
                    pass
                return False

        # 5. Text, Email, Tel, Number, URL, Textarea, Password
        locator = self._locate_element_for_field(page, field)
        if locator and locator.count() > 0:
            if self.is_submission_control(locator):
                self.blocked_actions.append(
                    f"Blocked automated fill on submission control for '{field.id or field.name or field.label}'"
                )
                return False

            locator.fill(answer_value)
            self.filled_fields.append(field.id or field.name or field.label)
            return True

        return False

    def _locate_submit_button(self, page: Page) -> Optional[Locator]:
        """Locate the portal submission button on the page."""
        # 1. Explicit submit input or button
        for sel in ('button[type="submit"]', 'input[type="submit"]'):
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    for i in range(loc.count()):
                        btn = loc.nth(i)
                        if btn.is_visible():
                            return btn
                    return loc.first
            except Exception:
                pass

        # 2. Buttons, input buttons, or ARIA role buttons with matching text/value/aria-label
        for sel in ("button", 'input[type="button"]', '[role="button"]', 'a[role="button"]'):
            try:
                loc = page.locator(sel)
                count = loc.count()
                for i in range(count):
                    btn = loc.nth(i)
                    text = (btn.text_content() or "").strip()
                    val = (btn.get_attribute("value") or "").strip()
                    aria = (btn.get_attribute("aria-label") or "").strip()
                    comb = f"{text} {val} {aria}".lower()
                    if SUBMISSION_INTERCEPT_PATTERN.search(comb):
                        return btn
            except Exception:
                pass

        # 3. Playwright role locator fallback
        try:
            btn = page.get_by_role("button", name=SUBMISSION_INTERCEPT_PATTERN)
            if btn.count() > 0:
                return btn.first
        except Exception:
            pass

        return None

    def _handle_resume_upload(
        self,
        page: Page,
        form_schema: FormSchema,
        resume_pdf_path: Union[Path, str],
    ) -> bool:
        """Helper to upload resume PDF to detected file inputs."""
        resume_path = Path(resume_pdf_path).resolve()
        if not resume_path.exists():
            return False

        # Try schema helper if available
        if hasattr(form_schema, "get_resume_upload_field"):
            resume_field = form_schema.get_resume_upload_field()
            if resume_field:
                loc = self._locate_element_for_field(page, resume_field)
                if loc and loc.count() > 0:
                    loc.set_input_files(str(resume_path))
                    self.uploaded_files.append(str(resume_path))
                    self.filled_fields.append(
                        resume_field.id or resume_field.name or resume_field.label
                    )
                    return True

        # Fallback to any file input on the page
        try:
            file_inputs = page.locator('input[type="file"]')
            if file_inputs.count() > 0:
                file_inputs.first.set_input_files(str(resume_path))
                self.uploaded_files.append(str(resume_path))
                self.filled_fields.append("resume_file_input")
                return True
        except Exception as exc:
            logger.debug("Fallback file upload exception: %s", exc)

        return False

    def fill_form(
        self,
        page: Page,
        form_schema: FormSchema,
        answers: dict[str, str],
        resume_pdf_path: Optional[Union[Path, str]] = None,
    ) -> str:
        """Connects to active Playwright page, safely fills form fields, uploads resume,

        and enforces safety by stopping before submission and returning
        STATUS_AWAITING_HUMAN_APPROVAL.
        """
        self.status = STATUS_FILLING
        self.filled_fields = []
        self.uploaded_files = []
        self.blocked_actions = []
        self.last_error = None

        if hasattr(page, "is_closed") and page.is_closed():
            self.status = STATUS_ERROR
            self.last_error = "Playwright page is closed."
            logger.error(self.last_error)
            return STATUS_ERROR

        try:
            # Check if form_schema is a dict
            if isinstance(form_schema, dict):
                form_schema = FormSchema(**form_schema)

            # Iterate and fill fields
            for field in getattr(form_schema, "fields", []):
                answer_val = self._find_answer_for_field(field, answers)
                if answer_val is not None:
                    self._fill_field(
                        page, field, answer_val, resume_pdf_path=resume_pdf_path
                    )
                elif (field.type or "").lower() == "file" and resume_pdf_path:
                    self._fill_field(page, field, "", resume_pdf_path=resume_pdf_path)

            # Upload resume if provided and not yet uploaded
            if resume_pdf_path and not self.uploaded_files:
                self._handle_resume_upload(page, form_schema, resume_pdf_path)

            # CRITICAL SAFETY ENFORCEMENT:
            # We never trigger final submission during fill_form.
            submit_btn = self._locate_submit_button(page)
            if submit_btn:
                logger.info(
                    "Submit button detected on form. Automated submission strictly halted for human approval."
                )

            # Always emit and return STATUS_AWAITING_HUMAN_APPROVAL
            self.status = STATUS_AWAITING_HUMAN_APPROVAL
            return STATUS_AWAITING_HUMAN_APPROVAL

        except Exception as exc:
            logger.exception("Error occurred while filling form: %s", exc)
            self.status = STATUS_ERROR
            self.last_error = str(exc)
            return STATUS_ERROR

    def submit_application_with_human_approval(
        self,
        page: Page,
        approval_token: str,
    ) -> bool:
        """Submits the application on the active portal ONLY when human approval is explicitly provided

        with a valid, non-empty approval token.
        """
        if hasattr(page, "is_closed") and page.is_closed():
            self.status = STATUS_ERROR
            self.last_error = "Submission failed: Playwright page is closed."
            logger.error(self.last_error)
            return False

        if not approval_token or not isinstance(approval_token, str) or not approval_token.strip():
            self.last_error = (
                "Submission rejected: A valid non-empty approval_token is required."
            )
            logger.error(self.last_error)
            raise ValueError(self.last_error)

        self.approval_token = approval_token.strip()

        submit_button = self._locate_submit_button(page)
        if not submit_button:
            self.status = STATUS_ERROR
            self.last_error = (
                "Submission failed: Portal submit button could not be located on the page."
            )
            logger.error(self.last_error)
            return False

        try:
            submit_button.scroll_into_view_if_needed()
            submit_button.click()
            self.status = STATUS_SUBMITTED
            logger.info("Application successfully submitted with human approval token.")
            return True
        except Exception as exc:
            self.status = STATUS_ERROR
            self.last_error = f"Error clicking submit button: {exc}"
            logger.exception(self.last_error)
            return False
