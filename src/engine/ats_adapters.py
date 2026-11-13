"""ATS Portal Specialist Adapters for JobApplier-Core.

Provides specialized form interaction adapters for career portals:
- Greenhouse (custom multi-selects, demographic radio groups, custom fields)
- Lever (drag-and-drop file upload wrappers, custom questions, URLs)
- Workday (multi-step progress wizard: Contact Info -> Experience -> Education -> Application Questions -> Review)
- Generic ATS fallback adapter

Includes multi-page form support, step detection, and strict human-in-the-loop (HITL)
safety enforcement: automated advance only navigates intermediate steps and NEVER triggers
final submission.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union
from bs4 import BeautifulSoup, Tag

try:
    from playwright.sync_api import Locator, Page
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    Locator = Any  # type: ignore
    Page = Any  # type: ignore

from src.engine.form_parser import (
    FormField,
    FormSchema,
    extract_form_schema_from_html,
    extract_form_schema_from_page,
)

logger = logging.getLogger(__name__)

# Navigation regex: intermediate steps (Next / Continue / Save & Continue)
NEXT_BUTTON_PATTERN = re.compile(
    r"\b(next|continue|save\s*(?:&|and)\s*continue|save\s*continue|proceed|next\s*step)\b",
    re.IGNORECASE,
)

# Submission regex: final application submit / apply actions
SUBMIT_BUTTON_PATTERN = re.compile(
    r"\b(submit|submit\s+application|apply|apply\s+now|send\s+application|complete\s+application|submit\s+my\s+application|finish)\b",
    re.IGNORECASE,
)

# Workday wizard step progression names
WORKDAY_KNOWN_STEPS = [
    "Contact Information",
    "My Information",
    "My Experience",
    "Experience",
    "Education",
    "Application Questions",
    "Voluntary Disclosures",
    "Self Identify",
    "Review",
    "Review and Submit",
]


def _is_playwright_page(obj: Any) -> bool:
    """Check if object behaves like a Playwright Page."""
    return hasattr(obj, "locator") and hasattr(obj, "evaluate") and hasattr(obj, "url")


def _clean_text(text: str | None) -> str:
    """Normalize whitespace and strip asterisk markers."""
    if not text:
        return ""
    cleaned = re.sub(r"[\*\u2731\u2217]", "", text)
    return re.sub(r"\s+", " ", cleaned).strip()


class BaseATSAdapter:
    """Base class for Applicant Tracking System (ATS) form adapters."""

    name: str = "generic"

    def detect(self, page_or_html: str | Page) -> bool:
        """Detect whether the current page or HTML snippet belongs to this ATS portal."""
        return False

    def extract_fields(self, page_or_html: str | Page) -> FormSchema:
        """Extract interactive form fields and schema from page or HTML snippet."""
        if _is_playwright_page(page_or_html):
            try:
                return extract_form_schema_from_page(page_or_html)
            except Exception as exc:
                logger.debug("Failed extracting schema from live page: %s", exc)
                return FormSchema(fields=[])

        if isinstance(page_or_html, str):
            if "<" in page_or_html:
                try:
                    return extract_form_schema_from_html(page_or_html)
                except Exception as exc:
                    logger.debug("Failed extracting schema from HTML string: %s", exc)
                    return FormSchema(fields=[])

        return FormSchema(fields=[])

    def fill_custom_controls(self, page: Page, answers: dict[str, str]) -> dict[str, bool]:
        """Fill portal-specific custom controls (custom selects, radios, file wrappers).

        Args:
            page: Active Playwright Page.
            answers: Mapping of field/question keys to answer strings.

        Returns:
            Dictionary mapping question/field keys to success boolean.
        """
        return {}

    def detect_form_step(self, page: Page | str) -> tuple[str, bool]:
        """Detect current form step and determine if it is the final submission step.

        Args:
            page: Playwright Page or raw HTML string.

        Returns:
            Tuple of (step_name_or_button, is_final_submit).
        """
        if isinstance(page, str):
            return self._detect_form_step_html(page)
        return self._detect_form_step_page(page)

    def _detect_form_step_html(self, html: str) -> tuple[str, bool]:
        """Generic fallback step detection from raw HTML string."""
        soup = BeautifulSoup(html, "html.parser")

        # Step indicator
        step_name = ""
        step_indicator = soup.select_one(
            '[aria-current="step"], [data-automation-id="activeStep"], .step.active, .wizard-step.active, .progress-step.active'
        )
        if step_indicator:
            step_name = _clean_text(step_indicator.get_text())

        # Inspect buttons
        has_next = False
        has_submit = False

        buttons = soup.find_all(["button", "input", "a"])
        for btn in buttons:
            tag = btn.name.lower()
            btn_type = (btn.get("type") or "").lower()
            text = _clean_text(btn.get_text() or btn.get("value") or "")
            if not text and tag == "input" and btn_type not in ("button", "submit"):
                continue

            if NEXT_BUTTON_PATTERN.search(text) and not SUBMIT_BUTTON_PATTERN.search(text):
                has_next = True
            elif SUBMIT_BUTTON_PATTERN.search(text) or btn_type == "submit":
                has_submit = True

        if not step_name:
            if has_next:
                step_name = "Intermediate Form Step"
            elif has_submit:
                step_name = "Submit Application"
            else:
                step_name = "Application Form"

        # Final submit if submit button present and no next button
        is_final = has_submit and not has_next
        return (step_name, is_final)

    def _detect_form_step_page(self, page: Page) -> tuple[str, bool]:
        """Generic fallback step detection from live Playwright Page."""
        step_name = ""

        # Step indicator
        try:
            indicator_loc = page.locator(
                '[aria-current="step"], [data-automation-id="activeStep"], .step.active, .wizard-step.active, .progress-step.active'
            ).first
            if indicator_loc.count() > 0 and indicator_loc.is_visible():
                step_name = _clean_text(indicator_loc.inner_text())
        except Exception:
            pass

        has_next = False
        has_submit = False

        try:
            buttons = page.locator("button, input[type='button'], input[type='submit'], a[role='button']")
            count = buttons.count()
            for i in range(count):
                btn = buttons.nth(i)
                if not btn.is_visible():
                    continue
                text = _clean_text(btn.inner_text() or btn.get_attribute("value") or "")
                btn_type = (btn.get_attribute("type") or "").lower()

                if NEXT_BUTTON_PATTERN.search(text) and not SUBMIT_BUTTON_PATTERN.search(text):
                    has_next = True
                elif SUBMIT_BUTTON_PATTERN.search(text) or btn_type == "submit":
                    has_submit = True
        except Exception as exc:
            logger.debug("Error inspecting page buttons in step detection: %s", exc)

        if not step_name:
            if has_next:
                step_name = "Intermediate Form Step"
            elif has_submit:
                step_name = "Submit Application"
            else:
                step_name = "Application Form"

        is_final = has_submit and not has_next
        return (step_name, is_final)

    def advance_step(self, page: Page) -> bool:
        """Advance to next form step only if intermediate; NEVER clicks final submit!

        Args:
            page: Active Playwright Page.

        Returns:
            True if an intermediate navigation step was clicked, False otherwise.
        """
        step_name, is_final = self.detect_form_step(page)
        if is_final:
            logger.warning(
                "advance_step intercepted: Current step '%s' is final submission. "
                "Automated submit clicks are strictly blocked for human approval safety.",
                step_name,
            )
            return False

        next_btn = self._find_next_button(page)
        if not next_btn:
            logger.info("advance_step: No intermediate next/continue button found on page.")
            return False

        try:
            btn_text = _clean_text(next_btn.inner_text() or next_btn.get_attribute("value") or "")
            # Safety double check: never click submit buttons
            if SUBMIT_BUTTON_PATTERN.search(btn_text) and not NEXT_BUTTON_PATTERN.search(btn_text):
                logger.warning(
                    "advance_step blocked: Button text '%s' matches submission pattern. Aborting click.",
                    btn_text,
                )
                return False

            if not next_btn.is_visible() or not next_btn.is_enabled():
                logger.info("advance_step: Next button is not visible or not enabled.")
                return False

            logger.info("advance_step: Clicking intermediate navigation button '%s'...", btn_text)
            next_btn.click()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=4000)
            except Exception:
                pass
            return True
        except Exception as exc:
            logger.error("advance_step encountered an error while clicking next: %s", exc)
            return False

    def _find_next_button(self, page: Page) -> Any:
        """Locate candidate next navigation button."""
        candidate_selectors = [
            'button[data-automation-id="pageFooterNextButton"]',
            'button[data-automation-id="bottom-navigation-next-button"]',
            'button[data-automation-id="next-button"]',
            'button[data-qa="btn-next"]',
            'button[id*="next" i]',
            'button[class*="next" i]',
            'button:has-text("Next")',
            'button:has-text("Save & Continue")',
            'button:has-text("Save and Continue")',
            'button:has-text("Continue")',
            'input[type="button"][value*="Next" i]',
            'input[type="button"][value*="Continue" i]',
        ]
        for sel in candidate_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    text = _clean_text(loc.inner_text() or loc.get_attribute("value") or "")
                    if not SUBMIT_BUTTON_PATTERN.search(text):
                        return loc
            except Exception:
                continue

        # Heuristic fallback: inspect all buttons
        try:
            buttons = page.locator("button, input[type='button'], input[type='submit']")
            count = buttons.count()
            for i in range(count):
                btn = buttons.nth(i)
                text = _clean_text(btn.inner_text() or btn.get_attribute("value") or "")
                if NEXT_BUTTON_PATTERN.search(text) and not SUBMIT_BUTTON_PATTERN.search(text):
                    if btn.is_visible() and btn.is_enabled():
                        return btn
        except Exception:
            pass

        return None


class GreenhouseAdapter(BaseATSAdapter):
    """Specialized adapter for Greenhouse (#application_form, multi-selects, demographic radios)."""

    name: str = "greenhouse"

    def detect(self, page_or_html: str | Page) -> bool:
        """Detect Greenhouse career portal."""
        if _is_playwright_page(page_or_html):
            try:
                url = page_or_html.url.lower()
                if "greenhouse.io" in url or "grnh.se" in url:
                    return True
                count = page_or_html.locator(
                    "#application_form, #apply_app, #submit_app, [name^='job_application[']"
                ).count()
                return count > 0
            except Exception:
                return False

        if isinstance(page_or_html, str):
            content = page_or_html.lower()
            if "greenhouse.io" in content or "grnh.se" in content:
                return True
            patterns = [
                r'id=["\']application_form["\']',
                r'id=["\']apply_app["\']',
                r'id=["\']submit_app["\']',
                r'name=["\']job_application\[',
                r'class=["\'][^"\']*greenhouse',
            ]
            return any(re.search(p, content) for p in patterns)

        return False

    def extract_fields(self, page_or_html: str | Page) -> FormSchema:
        """Extract fields and enrich with Greenhouse-specific hints."""
        schema = super().extract_fields(page_or_html)

        # Enhance field metadata for Greenhouse custom questions and demographics
        for field in schema.fields:
            name_lower = (field.name or "").lower()
            id_lower = (field.id or "").lower()
            label_lower = (field.label or "").lower()

            # Identify resume/cover letter uploads
            if "resume" in name_lower or "resume" in id_lower or "resume" in label_lower:
                field.type = "file"
            elif "cover_letter" in name_lower or "cover_letter" in id_lower:
                field.type = "file"

            # Identify demographic radio fields
            demographic_keywords = ["gender", "race", "ethnicity", "veteran", "disability"]
            if any(kw in name_lower or kw in id_lower for kw in demographic_keywords):
                if not field.type or field.type == "text":
                    field.type = "radio" if field.options else "select"

        return schema

    def fill_custom_controls(self, page: Page, answers: dict[str, str]) -> dict[str, bool]:
        """Fill Greenhouse custom multi-selects, demographic radio groups, and custom fields."""
        results: dict[str, bool] = {}

        for raw_key, value in answers.items():
            key = raw_key.strip().lower()
            val_clean = str(value).strip()
            if not val_clean:
                continue

            success = False

            # 1. Greenhouse Demographic Radio / Question controls
            demographic_keywords = ["gender", "race", "ethnicity", "veteran", "veteran_status", "disability", "disability_status"]
            matched_demographic = any(kw in key for kw in demographic_keywords)

            if matched_demographic:
                success = self._fill_demographic_field(page, key, val_clean)
                if success:
                    results[raw_key] = True
                    continue

            # 2. Greenhouse Select2 / Custom Multi-Selects
            success = self._fill_select2_or_multiselect(page, key, val_clean)
            if success:
                results[raw_key] = True
                continue

            # 3. Standard Greenhouse custom question attributes
            success = self._fill_custom_attribute_field(page, key, val_clean)
            results[raw_key] = success

        return results

    def _fill_demographic_field(self, page: Page, key: str, value: str) -> bool:
        """Handle Greenhouse EEO and demographic radio groups or dropdowns."""
        try:
            # Check for radio buttons matching demographic topic
            radios = page.locator(f"input[type='radio'][name*='{key}' i], #demographic_questions input[type='radio'], #eeoc_fields input[type='radio']")
            count = radios.count()
            if count > 0:
                for i in range(count):
                    radio = radios.nth(i)
                    radio_id = radio.get_attribute("id") or ""
                    radio_val = radio.get_attribute("value") or ""
                    # Check label associated with this radio
                    lbl_text = ""
                    if radio_id:
                        lbl_loc = page.locator(f"label[for='{radio_id}']")
                        if lbl_loc.count() > 0:
                            lbl_text = lbl_loc.first.inner_text()
                    if not lbl_text:
                        parent_lbl = radio.locator("xpath=..")
                        if parent_lbl.count() > 0:
                            lbl_text = parent_lbl.first.inner_text()

                    combined = f"{lbl_text} {radio_val}".strip().lower()
                    if value.lower() in combined or combined in value.lower():
                        radio.check(force=True)
                        return True

            # Check for demographic select dropdown
            select_loc = page.locator(f"select[name*='{key}' i], select[id*='{key}' i], #demographic_questions select, #eeoc_fields select").first
            if select_loc.count() > 0 and select_loc.is_visible():
                try:
                    select_loc.select_option(label=value)
                    return True
                except Exception:
                    select_loc.select_option(value=value)
                    return True
        except Exception as exc:
            logger.debug("Greenhouse demographic fill error for '%s': %s", key, exc)

        return False

    def _fill_select2_or_multiselect(self, page: Page, key: str, value: str) -> bool:
        """Handle Greenhouse Select2 containers or custom multi-select dropdowns."""
        try:
            # Check for select2 container associated with label or field
            s2_loc = page.locator(".select2-container").filter(
                has=page.locator(f"xpath=preceding::label[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{key}')]")
            ).first
            if s2_loc.count() == 0:
                s2_loc = page.locator(f"[id*='s2id_{key}' i], .select2-container").first

            if s2_loc.count() > 0 and s2_loc.is_visible():
                s2_loc.click()
                search_input = page.locator(".select2-input, input.select2-search__field").first
                if search_input.count() > 0 and search_input.is_visible():
                    search_input.fill(value)
                    # Pick matching result
                    match_opt = page.locator(".select2-result-label, .select2-results li").filter(
                        has_text=re.compile(re.escape(value), re.I)
                    ).first
                    if match_opt.count() > 0:
                        match_opt.click()
                        return True
                    search_input.press("Enter")
                    return True

            # Native multi-select fallback
            multi_select = page.locator(f"select[multiple][name*='{key}' i], select[multiple][id*='{key}' i]").first
            if multi_select.count() > 0:
                tokens = [t.strip() for t in value.split(",") if t.strip()]
                multi_select.select_option(tokens or [value])
                return True
        except Exception as exc:
            logger.debug("Greenhouse multi-select fill error for '%s': %s", key, exc)

        return False

    def _fill_custom_attribute_field(self, page: Page, key: str, value: str) -> bool:
        """Fill generic Greenhouse custom question inputs."""
        try:
            # Locate input by associated label text
            input_by_label = page.locator(
                f"//label[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{key}')]/following::input[1] | "
                f"//label[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{key}')]/following::textarea[1]"
            ).first
            if input_by_label.count() > 0 and input_by_label.is_visible():
                input_by_label.fill(value)
                return True
        except Exception:
            pass
        return False

    def detect_form_step(self, page: Page | str) -> tuple[str, bool]:
        """Detect Greenhouse form step (single-page form leading to submit)."""
        if isinstance(page, str):
            soup = BeautifulSoup(page, "html.parser")
            submit_btn = soup.select_one('#submit_app, input[type="submit"], button[type="submit"]')
            next_btn = soup.select_one('button[id*="next" i], input[value*="Next" i]')
            if next_btn and not submit_btn:
                return ("Greenhouse Application Step", False)
            return ("Greenhouse Application Form", True)

        try:
            has_submit = page.locator("#submit_app, input[type='submit'], button[type='submit']").count() > 0
            has_next = page.locator("button:has-text('Next'), input[value*='Next' i]").count() > 0
            if has_next and not has_submit:
                return ("Greenhouse Application Step", False)
            return ("Greenhouse Application Form", True)
        except Exception:
            return ("Greenhouse Application Form", True)

    def advance_step(self, page: Page) -> bool:
        """Greenhouse forms are single-step; NEVER clicks final submit!"""
        step_name, is_final = self.detect_form_step(page)
        if is_final:
            logger.warning(
                "advance_step intercepted: Greenhouse form is in final submission state ('%s'). "
                "Submit click strictly blocked.",
                step_name,
            )
            return False
        return super().advance_step(page)


class LeverAdapter(BaseATSAdapter):
    """Specialized adapter for Lever (.application-form, dropzone wrappers, custom questions)."""

    name: str = "lever"

    def detect(self, page_or_html: str | Page) -> bool:
        """Detect Lever career portal."""
        if _is_playwright_page(page_or_html):
            try:
                url = page_or_html.url.lower()
                if "jobs.lever.co" in url or "lever.co" in url:
                    return True
                count = page_or_html.locator(
                    ".application-form, #application-form, [data-qa='btn-apply'], [data-qa='btn-submit'], .lever-form"
                ).count()
                return count > 0
            except Exception:
                return False

        if isinstance(page_or_html, str):
            content = page_or_html.lower()
            if "jobs.lever.co" in content or "lever.co" in content:
                return True
            patterns = [
                r'class=["\'][^"\']*application-form',
                r'id=["\']application-form["\']',
                r'data-qa=["\']btn-submit["\']',
                r'data-qa=["\']btn-apply["\']',
                r'class=["\'][^"\']*lever-form',
            ]
            return any(re.search(p, content) for p in patterns)

        return False

    def extract_fields(self, page_or_html: str | Page) -> FormSchema:
        """Extract fields and enrich with Lever-specific hints."""
        schema = super().extract_fields(page_or_html)

        for field in schema.fields:
            name_lower = (field.name or "").lower()
            label_lower = (field.label or "").lower()

            if name_lower == "resume" or "resume" in label_lower:
                field.type = "file"
                field.required = True

            # Tag Lever social URLs
            if "urls[" in name_lower:
                field.type = "url"

        return schema

    def fill_custom_controls(self, page: Page, answers: dict[str, str]) -> dict[str, bool]:
        """Fill Lever dropzone file upload wrappers, URL fields, and custom questions."""
        results: dict[str, bool] = {}

        for raw_key, value in answers.items():
            key = raw_key.strip().lower()
            val_clean = str(value).strip()
            if not val_clean:
                continue

            success = False

            # 1. Lever File drag-and-drop input wrapper (Resume/CV upload)
            if key in ("resume", "resume_path", "cv", "attachment"):
                success = self._fill_lever_file_upload(page, val_clean)
                results[raw_key] = success
                continue

            # 2. Lever URL inputs (urls[LinkedIn], urls[GitHub], etc.)
            if any(url_type in key for url_type in ("linkedin", "github", "portfolio", "twitter", "other")):
                success = self._fill_lever_url_field(page, key, val_clean)
                if success:
                    results[raw_key] = True
                    continue

            # 3. Lever Custom Questions (.application-question)
            success = self._fill_lever_custom_question(page, key, val_clean)
            results[raw_key] = success

        return results

    def _fill_lever_file_upload(self, page: Page, file_path: str) -> bool:
        """Handle Lever drag-and-drop resume wrapper."""
        try:
            # File inputs inside .dropzone or named resume
            file_loc = page.locator(
                "input[name='resume'][type='file'], .dropzone input[type='file'], input[type='file']"
            ).first
            if file_loc.count() > 0:
                file_loc.set_input_files(file_path)
                return True
        except Exception as exc:
            logger.debug("Lever file upload fill error: %s", exc)
        return False

    def _fill_lever_url_field(self, page: Page, key: str, value: str) -> bool:
        """Fill Lever social/portfolio URL input controls."""
        try:
            for canonical in ["LinkedIn", "GitHub", "Portfolio", "Twitter", "Other"]:
                if canonical.lower() in key:
                    url_input = page.locator(
                        f"input[name='urls[{canonical}]'], input[name*='{canonical}' i]"
                    ).first
                    if url_input.count() > 0 and url_input.is_visible():
                        url_input.fill(value)
                        return True
        except Exception as exc:
            logger.debug("Lever URL field fill error for '%s': %s", key, exc)
        return False

    def _fill_lever_custom_question(self, page: Page, key: str, value: str) -> bool:
        """Fill Lever custom question within .application-question containers."""
        try:
            # Locate application question container matching text
            q_loc = page.locator(".application-question, .custom-question").filter(
                has_text=re.compile(re.escape(key), re.I)
            ).first
            if q_loc.count() > 0:
                # Text input or textarea
                txt_input = q_loc.locator("input[type='text'], textarea").first
                if txt_input.count() > 0 and txt_input.is_visible():
                    txt_input.fill(value)
                    return True

                # Select dropdown
                select_elem = q_loc.locator("select").first
                if select_elem.count() > 0 and select_elem.is_visible():
                    try:
                        select_elem.select_option(label=value)
                        return True
                    except Exception:
                        select_elem.select_option(value=value)
                        return True

                # Radio button or checkbox option
                opt = q_loc.locator("input[type='radio'], input[type='checkbox']").first
                if opt.count() > 0:
                    opt.check(force=True)
                    return True
        except Exception as exc:
            logger.debug("Lever custom question error for '%s': %s", key, exc)

        return False

    def detect_form_step(self, page: Page | str) -> tuple[str, bool]:
        """Detect Lever form step (single-page form leading to submit)."""
        if isinstance(page, str):
            soup = BeautifulSoup(page, "html.parser")
            submit_btn = soup.select_one('[data-qa="btn-submit"], .template-btn-submit, button[type="submit"]')
            next_btn = soup.select_one('button[id*="next" i], input[value*="Next" i]')
            if next_btn and not submit_btn:
                return ("Lever Application Step", False)
            return ("Lever Application Form", True)

        try:
            has_submit = page.locator(
                "[data-qa='btn-submit'], .template-btn-submit, button[type='submit']"
            ).count() > 0
            has_next = page.locator("button:has-text('Next'), input[value*='Next' i]").count() > 0
            if has_next and not has_submit:
                return ("Lever Application Step", False)
            return ("Lever Application Form", True)
        except Exception:
            return ("Lever Application Form", True)

    def advance_step(self, page: Page) -> bool:
        """Lever forms are single-step; NEVER clicks final submit!"""
        step_name, is_final = self.detect_form_step(page)
        if is_final:
            logger.warning(
                "advance_step intercepted: Lever form is in final submission state ('%s'). "
                "Submit click strictly blocked.",
                step_name,
            )
            return False
        return super().advance_step(page)


class WorkdayAdapter(BaseATSAdapter):
    """Specialized adapter for Workday multi-step progress wizard and custom controls.

    Wizard steps: Contact Info -> Experience -> Education -> Application Questions -> Review.
    """

    name: str = "workday"

    def detect(self, page_or_html: str | Page) -> bool:
        """Detect Workday career portal."""
        if _is_playwright_page(page_or_html):
            try:
                url = page_or_html.url.lower()
                if "myworkdayjobs.com" in url or "workday.com" in url:
                    return True
                count = page_or_html.locator(
                    "[data-automation-id='workdayApplication'], [data-automation-id='progressBar'], "
                    "[data-automation-id='pageFooterNextButton'], [data-automation-id='pageFooterSubmitButton']"
                ).count()
                return count > 0
            except Exception:
                return False

        if isinstance(page_or_html, str):
            content = page_or_html.lower()
            if "myworkdayjobs.com" in content or "workday.com" in content:
                return True
            patterns = [
                r'data-automation-id=["\']workdayApplication["\']',
                r'data-automation-id=["\']progressBar["\']',
                r'data-automation-id=["\']pageFooterNextButton["\']',
                r'data-automation-id=["\']pageFooterSubmitButton["\']',
                r'data-automation-id=["\']jobApplicationPage["\']',
            ]
            return any(re.search(p, content, re.I) for p in patterns)

        return False

    def extract_fields(self, page_or_html: str | Page) -> FormSchema:
        """Extract fields and enrich with Workday data-automation-id mappings."""
        schema = super().extract_fields(page_or_html)

        # Workday fields heavily rely on data-automation-id attributes
        if isinstance(page_or_html, str) and "<" in page_or_html:
            soup = BeautifulSoup(page_or_html, "html.parser")
            for field in schema.fields:
                if not field.id or field.id.startswith("field_"):
                    # Find matching tag by name or label
                    elem = soup.find(attrs={"name": field.name}) or soup.find(id=field.id)
                    if elem and elem.get("data-automation-id"):
                        field.id = elem.get("data-automation-id")

        return schema

    def detect_form_step(self, page: Page | str) -> tuple[str, bool]:
        """Detect current step in Workday multi-step progress wizard.

        Returns:
            Tuple of (step_name, is_final_submit).
        """
        if isinstance(page, str):
            return self._detect_workday_step_html(page)
        return self._detect_workday_step_page(page)

    def _detect_workday_step_html(self, html: str) -> tuple[str, bool]:
        """Detect Workday step from raw HTML string."""
        soup = BeautifulSoup(html, "html.parser")

        # 1. Check active step in progress bar
        step_name = ""
        active_step_el = soup.select_one(
            '[data-automation-id="activeStep"], '
            '[data-automation-id="progressStep"][aria-current="step"], '
            'li[aria-current="step"], .activeStep'
        )
        if active_step_el:
            step_name = _clean_text(active_step_el.get_text())

        # 2. Check footer buttons
        has_submit_btn = bool(
            soup.select_one(
                'button[data-automation-id="pageFooterSubmitButton"], '
                'button[data-automation-id="submit-button"], '
                'button[data-automation-id="bottom-navigation-submit-button"]'
            )
        )
        has_next_btn = bool(
            soup.select_one(
                'button[data-automation-id="pageFooterNextButton"], '
                'button[data-automation-id="bottom-navigation-next-button"], '
                'button[data-automation-id="next-button"]'
            )
        )

        if not step_name:
            # Fallback to header or button heuristic
            step_header = soup.select_one('[data-automation-id="jobApplicationPage"] h2, h2')
            if step_header:
                step_name = _clean_text(step_header.get_text())

        # Final submit determination:
        # Step name is Review or submit button present without next button
        is_review_step = any(kw in (step_name or "").lower() for kw in ("review", "summary"))
        is_final = is_review_step or (has_submit_btn and not has_next_btn)

        if not step_name:
            step_name = "Review" if is_final else "Workday Wizard Step"

        return (step_name, is_final)

    def _detect_workday_step_page(self, page: Page) -> tuple[str, bool]:
        """Detect Workday step from live Playwright Page."""
        step_name = ""

        try:
            active_step_loc = page.locator(
                '[data-automation-id="activeStep"], '
                '[data-automation-id="progressStep"][aria-current="step"], '
                'li[aria-current="step"]'
            ).first
            if active_step_loc.count() > 0 and active_step_loc.is_visible():
                step_name = _clean_text(active_step_loc.inner_text())
        except Exception:
            pass

        has_submit_btn = False
        has_next_btn = False

        try:
            submit_loc = page.locator(
                'button[data-automation-id="pageFooterSubmitButton"], '
                'button[data-automation-id="submit-button"], '
                'button[data-automation-id="bottom-navigation-submit-button"]'
            ).first
            if submit_loc.count() > 0 and submit_loc.is_visible() and submit_loc.is_enabled():
                has_submit_btn = True

            next_loc = page.locator(
                'button[data-automation-id="pageFooterNextButton"], '
                'button[data-automation-id="bottom-navigation-next-button"], '
                'button[data-automation-id="next-button"]'
            ).first
            if next_loc.count() > 0 and next_loc.is_visible() and next_loc.is_enabled():
                has_next_btn = True
        except Exception as exc:
            logger.debug("Error checking Workday buttons: %s", exc)

        is_review_step = any(kw in (step_name or "").lower() for kw in ("review", "summary"))
        is_final = is_review_step or (has_submit_btn and not has_next_btn)

        if not step_name:
            step_name = "Review" if is_final else "Workday Wizard Step"

        return (step_name, is_final)

    def advance_step(self, page: Page) -> bool:
        """Advance to next Workday wizard step; NEVER clicks final submit button!"""
        step_name, is_final = self.detect_form_step(page)
        if is_final:
            logger.warning(
                "advance_step intercepted: Workday wizard is on final review/submission step ('%s'). "
                "Submit click strictly blocked for human approval safety.",
                step_name,
            )
            return False

        # Workday-specific next button locators
        workday_next_selectors = [
            'button[data-automation-id="pageFooterNextButton"]',
            'button[data-automation-id="bottom-navigation-next-button"]',
            'button[data-automation-id="next-button"]',
        ]
        for sel in workday_next_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible() and btn.is_enabled():
                    btn_text = _clean_text(btn.inner_text())
                    logger.info("advance_step: Advancing Workday step via '%s' (%s)...", sel, btn_text)
                    btn.click()
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=4000)
                    except Exception:
                        pass
                    return True
            except Exception as exc:
                logger.debug("Workday next button selector '%s' failed: %s", sel, exc)

        # Fallback to base advance logic
        return super().advance_step(page)

    def fill_custom_controls(self, page: Page, answers: dict[str, str]) -> dict[str, bool]:
        """Fill Workday custom comboboxes, searchable dropdowns, and radio groups."""
        results: dict[str, bool] = {}

        for raw_key, value in answers.items():
            key = raw_key.strip().lower()
            val_clean = str(value).strip()
            if not val_clean:
                continue

            success = False

            # 1. Workday custom searchable combobox dropdown
            success = self._fill_workday_combobox(page, key, val_clean)
            if success:
                results[raw_key] = True
                continue

            # 2. Workday radio button groups
            success = self._fill_workday_radio_group(page, key, val_clean)
            results[raw_key] = success

        return results

    def _fill_workday_combobox(self, page: Page, key: str, value: str) -> bool:
        """Handle Workday searchable dropdown and combobox popups."""
        try:
            # Look for combobox container matching field key
            combo_loc = page.locator(
                f"[data-automation-id='select-item'], [role='combobox'], [data-automation-id*='{key}' i]"
            ).first
            if combo_loc.count() > 0 and combo_loc.is_visible():
                combo_loc.click()

                # If search input appears inside dropdown
                search_input = page.locator(
                    "input[data-automation-id='search-input'], input[type='search'], [role='combobox'] input"
                ).first
                if search_input.count() > 0 and search_input.is_visible():
                    search_input.fill(value)

                # Select matching option from popup list
                opt_loc = page.locator(
                    "[role='option'], [data-automation-id='select-option'], div[data-automation-id*='select-option']"
                ).filter(has_text=re.compile(re.escape(value), re.I)).first
                if opt_loc.count() > 0:
                    opt_loc.click()
                    return True
        except Exception as exc:
            logger.debug("Workday combobox fill error for '%s': %s", key, exc)

        return False

    def _fill_workday_radio_group(self, page: Page, key: str, value: str) -> bool:
        """Handle Workday radio button groups."""
        try:
            group_loc = page.locator(
                f"[data-automation-id='radio-button-group'], fieldset"
            ).filter(has_text=re.compile(re.escape(key), re.I)).first
            if group_loc.count() > 0:
                radio_opt = group_loc.locator(
                    f"label, [data-automation-id='radio-button']"
                ).filter(has_text=re.compile(re.escape(value), re.I)).first
                if radio_opt.count() > 0:
                    radio_opt.click()
                    return True
        except Exception as exc:
            logger.debug("Workday radio group fill error for '%s': %s", key, exc)

        return False


class GenericATSAdapter(BaseATSAdapter):
    """Fallback generic adapter for custom and unclassified career portals."""

    name: str = "generic"

    def detect(self, page_or_html: str | Page) -> bool:
        """Fallback adapter detects any portal."""
        return True


def get_ats_adapter(url_or_html: str, page: Page | None = None) -> BaseATSAdapter:
    """Detect and return the appropriate ATS adapter for a given URL, HTML snippet, or Page.

    Tries specialized adapters (WorkdayAdapter, GreenhouseAdapter, LeverAdapter) in priority order,
    falling back to GenericATSAdapter.

    Args:
        url_or_html: Portal URL or raw HTML string.
        page: Optional live Playwright Page instance.

    Returns:
        BaseATSAdapter instance (WorkdayAdapter, GreenhouseAdapter, LeverAdapter, or GenericATSAdapter).
    """
    adapters: list[BaseATSAdapter] = [
        WorkdayAdapter(),
        GreenhouseAdapter(),
        LeverAdapter(),
    ]

    target: Any = page if page is not None else url_or_html
    for adapter in adapters:
        try:
            if adapter.detect(target):
                return adapter
        except Exception as exc:
            logger.debug("Adapter %s detection failed: %s", adapter.name, exc)
            continue

    # If page was provided but didn't match, also test url_or_html
    if page is not None and url_or_html:
        for adapter in adapters:
            try:
                if adapter.detect(url_or_html):
                    return adapter
            except Exception as exc:
                logger.debug("Adapter %s URL detection failed: %s", adapter.name, exc)
                continue

    return GenericATSAdapter()


__all__ = [
    "BaseATSAdapter",
    "GreenhouseAdapter",
    "LeverAdapter",
    "WorkdayAdapter",
    "GenericATSAdapter",
    "get_ats_adapter",
]
