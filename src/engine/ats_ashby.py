"""Ashby ATS Portal Adapter for JobApplier-Core.

Provides specialized detection, DOM schema extraction, custom React control
filling, and multi-step navigation for Ashby (jobs.ashbyhq.com) job boards
with strict human-in-the-loop (HITL) final submission blocking.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

try:
    from playwright.sync_api import Locator, Page
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    Locator = Any  # type: ignore
    Page = Any  # type: ignore

from src.engine.ats_adapters import (
    NEXT_BUTTON_PATTERN,
    SUBMIT_BUTTON_PATTERN,
    BaseATSAdapter,
    _clean_text,
    _is_playwright_page,
)
from src.engine.form_parser import FormField, FormSchema

logger = logging.getLogger(__name__)

_ASHBY_CONTAINER_RE = re.compile(
    r"ashby-application-form-field|ashby-field|ashby-question|_ashby_field|_ashby_question|"
    r"_ashby_dropzone|ashby-dropzone|form-group|form-field|input-group|question|field",
    re.IGNORECASE,
)

_ASHBY_LABEL_HINT_RE = re.compile(
    r"question-title|field-title|label|prompt|title",
    re.IGNORECASE,
)

_ASHBY_URL_FIELD_RE = re.compile(
    r"linkedin|github|portfolio|website|personal[_ -]?site|twitter|url",
    re.IGNORECASE,
)

_ASHBY_EMAIL_HINT_RE = re.compile(r"\bemail\b|e-mail|_ashby_email", re.IGNORECASE)
_ASHBY_PHONE_HINT_RE = re.compile(r"\bphone\b|\bmobile\b|\btel\b|_ashby_phone", re.IGNORECASE)

_SUBMIT_ATTR_RE = re.compile(
    r"(?:submit|apply[-_ ]?now|send[-_ ]?application|complete[-_ ]?application)",
    re.IGNORECASE,
)


def _escape_css_attr(value: str) -> str:
    """Escape a string for safe inclusion inside double-quoted CSS attribute selectors."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _normalize_key(value: str) -> str:
    """Normalize identifier or label strings for resilient matching."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _is_submit_like(
    text: str = "",
    btn_type: str = "",
    elem_id: str = "",
    elem_class: str = "",
    aria_label: str = "",
    data_testid: str = "",
) -> bool:
    """Return True if control metadata looks like a final submission action."""
    if btn_type.strip().lower() == "submit":
        return True
    visible_text = _clean_text(text)
    if SUBMIT_BUTTON_PATTERN.search(visible_text) and not NEXT_BUTTON_PATTERN.search(visible_text):
        return True
    if aria_label and SUBMIT_BUTTON_PATTERN.search(aria_label) and not NEXT_BUTTON_PATTERN.search(aria_label):
        return True
    attr_blob = f"{elem_id} {elem_class} {data_testid}"
    if _SUBMIT_ATTR_RE.search(attr_blob) and not NEXT_BUTTON_PATTERN.search(visible_text):
        return True
    return False


def _extract_container_question_label(container: Tag | None, exclude_tags: tuple[str, ...] = ()) -> str:
    """Extract the primary question label from an Ashby field wrapper container."""
    if container is None:
        return ""

    legend = container.find("legend")
    if legend:
        text = _clean_text(legend.get_text())
        if text:
            return text

    for candidate in container.find_all(["label", "span", "p", "h2", "h3", "h4", "div"]):
        if exclude_tags and candidate.find(list(exclude_tags)):
            # Skip wrapper labels/divs that wrap the interactive options themselves
            if candidate.name == "label" or candidate.find("input"):
                continue
        classes = " ".join(candidate.get("class") or [])
        if candidate.name in ("label", "h2", "h3", "h4") or _ASHBY_LABEL_HINT_RE.search(classes):
            text = _clean_text(candidate.get_text())
            if text:
                return text

    return ""


def _container_has_required_marker(container: Tag | None, el: Tag | None = None) -> bool:
    """Check whether a field element or its enclosing Ashby container marks it required."""
    if el is not None:
        if el.has_attr("required") or (el.get("aria-required") or "").lower() == "true":
            return True
    if container is None:
        return False
    if (container.get("aria-required") or "").lower() == "true":
        return True
    if container.select_one('.required, [class*="asterisk" i], [aria-required="true"]'):
        return True
    label_el = container.find("label") or container.find(class_=_ASHBY_LABEL_HINT_RE)
    if label_el:
        raw_text = label_el.get_text()
        if re.search(r"[\*\u2731\u2217]", raw_text):
            return True
    return False


class AshbyAdapter(BaseATSAdapter):
    """Specialized ATS adapter for Ashby (jobs.ashbyhq.com) React application forms."""

    name: str = "ashby"

    def detect(self, page_or_html: str | Page) -> bool:
        """Detect Ashby job boards from live Page URL/DOM or raw HTML/URL string."""
        if _is_playwright_page(page_or_html):
            try:
                url = (page_or_html.url or "").lower()
                if "jobs.ashbyhq.com" in url or "ashbyhq.com" in url:
                    return True
                count = page_or_html.locator(
                    "[name*='_ashby_'], [id*='_ashby_'], [class*='_ashby_'], "
                    "[class*='ashby-application-form'], [class*='ashby-job-posting'], "
                    "[class*='ashby-portal'], [class*='ashby-form'], "
                    "[id*='ashby-'], [data-ashby], [data-testid*='ashby' i]"
                ).count()
                return count > 0
            except Exception:
                return False

        if isinstance(page_or_html, str):
            content = page_or_html.lower()
            if "jobs.ashbyhq.com" in content or "ashbyhq.com" in content:
                return True
            patterns = [
                r"_ashby_[a-z0-9_-]*",
                r'class=["\'][^"\']*ashby-(?:application|job|portal|form|field|question|dropzone|select|radio|checkbox)',
                r'id=["\'][^"\']*ashby[-_]',
                r"\bdata-ashby\b",
                r'data-testid=["\'][^"\']*ashby',
            ]
            return any(re.search(pattern, content) for pattern in patterns)

        return False

    def extract_fields(self, page_or_html: str | Page) -> FormSchema:
        """Extract Ashby inputs, custom controls, radiogroups, dropzones, and textareas."""
        schema = super().extract_fields(page_or_html)
        html_content = ""
        if isinstance(page_or_html, str) and "<" in page_or_html:
            html_content = page_or_html
        elif _is_playwright_page(page_or_html):
            try:
                html_content = page_or_html.content()
            except Exception:
                html_content = ""

        if not html_content:
            return self._enrich_fields_without_soup(schema)

        soup = BeautifulSoup(html_content, "html.parser")
        self._merge_role_radiogroups(soup, schema)
        self._fix_native_radio_labels(soup, schema)
        self._enrich_combobox_and_multiselects(soup, schema)
        self._enrich_dropzones_and_files(soup, schema)
        self._enrich_standard_ashby_fields(soup, schema)
        self._extract_contenteditable_textareas(soup, schema)
        return schema

    def _enrich_fields_without_soup(self, schema: FormSchema) -> FormSchema:
        """Apply lightweight type inference when HTML markup is unavailable."""
        for field in schema.fields:
            combined = f"{field.id} {field.name} {field.label}".lower()
            if any(k in combined for k in ("resume", "curriculum vitae", "cover_letter", "cover letter", "dropzone")):
                if field.type in ("text", "file"):
                    field.type = "file"
            elif field.type == "text":
                if _ASHBY_URL_FIELD_RE.search(combined):
                    field.type = "url"
                elif _ASHBY_EMAIL_HINT_RE.search(combined):
                    field.type = "email"
                elif _ASHBY_PHONE_HINT_RE.search(combined):
                    field.type = "tel"
        return schema

    def _find_soup_element(self, soup: BeautifulSoup, field: FormField) -> Tag | None:
        """Locate the BeautifulSoup tag matching a FormField."""
        if field.id:
            tag = soup.find(id=field.id)
            if isinstance(tag, Tag):
                return tag
        if field.name:
            tag = soup.find(attrs={"name": field.name})
            if isinstance(tag, Tag):
                return tag
        return None

    def _find_ashby_container(self, el: Tag | None) -> Tag | None:
        """Walk up DOM to find the enclosing Ashby field or question wrapper."""
        if el is None:
            return None
        return el.find_parent(class_=_ASHBY_CONTAINER_RE) or el.find_parent("fieldset")

    def _merge_role_radiogroups(self, soup: BeautifulSoup, schema: FormSchema) -> None:
        """Consolidate ARIA radiogroups and Ashby boolean toggle groups into single radio fields."""
        group_containers = soup.find_all(
            lambda t: isinstance(t, Tag)
            and (
                t.get("role") == "radiogroup"
                or any(
                    re.search(r"_ashby_(?:radio|boolean)|ashby-(?:radio|boolean)", c, re.I)
                    for c in (t.get("class") or [])
                )
            )
        )

        for group in group_containers:
            # Skip if this group solely wraps native <input type="radio"> handled elsewhere
            native_radios = group.find_all("input", attrs={"type": "radio"})
            role_radios = group.find_all(attrs={"role": "radio"})
            bool_buttons = [
                btn
                for btn in group.find_all("button")
                if not _is_submit_like(
                    text=btn.get_text(),
                    btn_type=btn.get("type") or "",
                    elem_id=btn.get("id") or "",
                    elem_class=" ".join(btn.get("class") or []),
                )
            ]

            if native_radios and not role_radios:
                continue
            option_nodes = role_radios or bool_buttons
            if not option_nodes:
                continue

            # Remove separate radio fields emitted for individual role="radio" buttons
            option_ids = {node.get("id") for node in option_nodes if node.get("id")}
            option_names = {node.get("name") for node in option_nodes if node.get("name")}
            option_texts = {_clean_text(node.get_text()) for node in option_nodes if _clean_text(node.get_text())}

            schema.fields = [
                f
                for f in schema.fields
                if not (
                    f.type == "radio"
                    and (
                        (f.id and f.id in option_ids)
                        or (f.name and f.name in option_names)
                        or (not f.name and f.label in option_texts)
                    )
                )
            ]

            wrapper = self._find_ashby_container(group) or group
            group_label = ""
            if group.get("aria-label"):
                group_label = _clean_text(group.get("aria-label"))
            elif group.get("aria-labelledby"):
                refs = str(group.get("aria-labelledby")).split()
                texts = [soup.find(id=r).get_text() for r in refs if soup.find(id=r)]
                group_label = _clean_text(" ".join(texts))
            if not group_label:
                group_label = _extract_container_question_label(wrapper, exclude_tags=("button", "input"))

            group_id = group.get("id") or group.get("data-field") or (next(iter(option_ids), ""))
            group_name = group.get("data-name") or group.get("data-field") or group_id
            options: list[str] = []
            selected_val = ""
            for node in option_nodes:
                opt_text = _clean_text(node.get_text() or node.get("aria-label") or node.get("data-value") or "")
                if opt_text and opt_text not in options:
                    options.append(opt_text)
                if (node.get("aria-checked") or "").lower() == "true" or (node.get("data-selected") or "").lower() == "true":
                    selected_val = opt_text

            if not group_id and group_label:
                group_id = re.sub(r"[^a-z0-9]+", "_", group_label.lower()).strip("_")

            existing = schema.get_field_by_id(group_id) if group_id else None
            if existing is not None:
                existing.type = "radio"
                existing.options = options or existing.options
                if group_label:
                    existing.label = group_label
                existing.required = existing.required or _container_has_required_marker(wrapper, group)
            else:
                schema.fields.append(
                    FormField(
                        id=group_id or f"ashby_radio_{len(schema.fields)}",
                        name=group_name or group_id,
                        label=group_label or group_name or group_id,
                        type="radio",
                        required=_container_has_required_marker(wrapper, group),
                        options=options,
                        value=selected_val,
                        aria_label=group.get("aria-label") or "",
                    )
                )

    def _fix_native_radio_labels(self, soup: BeautifulSoup, schema: FormSchema) -> None:
        """Resolve accurate question titles for native Ashby radio groups outside fieldsets."""
        for field in schema.fields:
            if field.type != "radio" or not field.name:
                continue
            radios = soup.find_all("input", attrs={"type": "radio", "name": field.name})
            if not radios:
                continue

            options: list[str] = []
            for r in radios:
                opt_label = ""
                rid = r.get("id")
                if rid:
                    lbl = soup.find("label", attrs={"for": rid})
                    if lbl:
                        opt_label = _clean_text(lbl.get_text())
                if not opt_label:
                    parent_lbl = r.find_parent("label")
                    if parent_lbl:
                        opt_label = _clean_text(parent_lbl.get_text())
                if not opt_label:
                    opt_label = _clean_text(r.get("value") or "")
                if opt_label and opt_label not in options:
                    options.append(opt_label)

            if options:
                field.options = options

            container = self._find_ashby_container(radios[0])
            question_label = _extract_container_question_label(container, exclude_tags=("input",))
            if question_label and question_label not in options:
                field.label = question_label
            if _container_has_required_marker(container, radios[0]):
                field.required = True

    def _collect_listbox_options(self, soup: BeautifulSoup, control: Tag, container: Tag | None) -> list[str]:
        """Collect option strings from linked ARIA listboxes, sibling menus, or data attributes."""
        options: list[str] = []

        def _add_opt(text: str) -> None:
            cleaned = _clean_text(text)
            if cleaned and cleaned not in options:
                options.append(cleaned)

        # 1. Explicit data-options JSON attribute
        raw_data_opts = control.get("data-options") or (container.get("data-options") if container else None)
        if raw_data_opts:
            try:
                parsed = json.loads(raw_data_opts)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict):
                            _add_opt(str(item.get("label") or item.get("value") or ""))
                        else:
                            _add_opt(str(item))
            except Exception:
                for token in str(raw_data_opts).split(","):
                    _add_opt(token)

        # 2. Linked listbox via aria-controls or aria-owns
        for attr in ("aria-controls", "aria-owns"):
            target_ids = str(control.get(attr) or "").split()
            for tid in target_ids:
                menu = soup.find(id=tid)
                if menu:
                    for opt in menu.find_all(attrs={"role": "option"}) or menu.find_all(["li", "option"]):
                        _add_opt(opt.get_text() or opt.get("data-value") or "")

        # 3. Options inside the same field container
        search_root = container or control
        for opt in search_root.find_all(attrs={"role": "option"}):
            _add_opt(opt.get_text() or opt.get("data-value") or "")

        if not options and search_root is not None:
            for opt in search_root.select(
                "[class*='_ashby_option' i], [class*='ashby-select-option' i], "
                "[class*='ashby-option' i], ul[role='listbox'] li"
            ):
                _add_opt(opt.get_text() or opt.get("data-value") or "")

        # 4. Checkbox-backed custom multi-select inside explicit multiselect container
        if not options and container is not None:
            container_classes = " ".join(container.get("class") or []).lower()
            if "multiselect" in container_classes or "multi-select" in container_classes:
                for cb in container.find_all("input", attrs={"type": "checkbox"}):
                    parent_lbl = cb.find_parent("label")
                    text = parent_lbl.get_text() if parent_lbl else (cb.get("value") or "")
                    _add_opt(text)

        return options

    def _enrich_combobox_and_multiselects(self, soup: BeautifulSoup, schema: FormSchema) -> None:
        """Enrich ARIA comboboxes, linked listboxes, and custom Ashby select/multi-select controls."""
        # Deduplicate wrapper <div role="combobox"> that also contains an extracted <input>
        combobox_divs = soup.find_all(
            lambda t: isinstance(t, Tag) and t.name != "input" and t.get("role") in ("combobox", "listbox")
        )
        for div in combobox_divs:
            inner_input = div.find("input")
            if inner_input is not None:
                inner_id = inner_input.get("id") or ""
                inner_name = inner_input.get("name") or ""
                div_id = div.get("id") or ""
                if inner_id and div_id and inner_id != div_id:
                    # Prefer one canonical select entry for the combobox
                    schema.fields = [
                        f for f in schema.fields if not (f.id == inner_id and f.type == "text")
                    ]

        # Enrich existing select/combobox fields with sibling/linked listbox options
        for field in schema.fields:
            el = self._find_soup_element(soup, field)
            if el is None:
                continue
            container = self._find_ashby_container(el)
            is_combobox = (
                field.type == "select"
                or el.get("role") in ("combobox", "listbox")
                or (el.get("aria-haspopup") or "").lower() == "listbox"
                or any(
                    re.search(r"_ashby_(?:select|combobox|multiselect)|ashby-(?:select|combobox|multiselect)", c, re.I)
                    for c in (el.get("class") or [])
                )
            )
            if is_combobox:
                field.type = "select"
                if not field.options:
                    field.options = self._collect_listbox_options(soup, el, container)

        # Discover custom Ashby select / combobox / multiselect wrappers missed by base parser
        custom_selects = soup.find_all(
            lambda t: isinstance(t, Tag)
            and (
                (t.get("aria-haspopup") or "").lower() == "listbox"
                or any(
                    re.search(
                        r"_ashby_(?:select|combobox|multiselect)|ashby-(?:select|combobox|multiselect)",
                        c,
                        re.I,
                    )
                    for c in (t.get("class") or [])
                )
            )
        )
        for ctrl in custom_selects:
            # Skip option items or internal sub-elements
            classes = " ".join(ctrl.get("class") or []).lower()
            if "option" in classes or "menu" in classes or "dropdown-list" in classes:
                continue

            # If it wraps an already extracted <select>, <input>, or [role="combobox"], skip adding duplicate
            inner_native = ctrl.find(["select", "input"]) or ctrl.find(attrs={"role": "combobox"})
            if inner_native is not None:
                inner_id = inner_native.get("id") or inner_native.get("name") or ""
                if inner_id and schema.get_field_by_id(inner_id):
                    # Ensure options from the wrapper are attached to existing field
                    existing = schema.get_field_by_id(inner_id)
                    if existing and not existing.options:
                        existing.options = self._collect_listbox_options(soup, ctrl, self._find_ashby_container(ctrl))
                        existing.type = "select"
                    continue

            ctrl_id = ctrl.get("id") or ctrl.get("data-field") or ""
            ctrl_name = ctrl.get("name") or ctrl.get("data-name") or ctrl_id
            if ctrl_id and schema.get_field_by_id(ctrl_id):
                continue
            if ctrl_name and schema.get_field_by_id(ctrl_name):
                continue

            container = self._find_ashby_container(ctrl) or ctrl
            label = (
                _clean_text(ctrl.get("aria-label"))
                or _extract_container_question_label(container, exclude_tags=("select", "input", "ul"))
            )
            options = self._collect_listbox_options(soup, ctrl, container)
            if not ctrl_id and label:
                ctrl_id = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")

            # If multiselect uses checkboxes inside this container, consolidate them
            if "multiselect" in classes or "multi-select" in classes:
                cb_names = {
                    cb.get("name") or cb.get("id")
                    for cb in container.find_all("input", attrs={"type": "checkbox"})
                    if (cb.get("name") or cb.get("id"))
                }
                if cb_names and options:
                    schema.fields = [
                        f for f in schema.fields if not (f.type == "checkbox" and (f.id in cb_names or f.name in cb_names))
                    ]

            schema.fields.append(
                FormField(
                    id=ctrl_id or f"ashby_select_{len(schema.fields)}",
                    name=ctrl_name or ctrl_id,
                    label=label or ctrl_name or ctrl_id,
                    type="select",
                    required=_container_has_required_marker(container, ctrl),
                    options=options,
                    aria_label=ctrl.get("aria-label") or "",
                )
            )

    def _enrich_dropzones_and_files(self, soup: BeautifulSoup, schema: FormSchema) -> None:
        """Ensure Ashby file dropzones and resume/CV inputs are typed and labeled cleanly."""
        for field in schema.fields:
            el = self._find_soup_element(soup, field)
            combined = f"{field.id} {field.name} {field.label}".lower()
            if field.type == "file" or (
                el is not None and el.name == "input" and (el.get("type") or "").lower() == "file"
            ):
                field.type = "file"
                container = self._find_ashby_container(el)
                if container is not None:
                    q_label = _extract_container_question_label(container, exclude_tags=("input",))
                    if q_label and (
                        not field.label
                        or "drag and drop" in field.label.lower()
                        or field.label.lower().startswith("_ashby")
                    ):
                        field.label = q_label
                    if _container_has_required_marker(container, el):
                        field.required = True
            elif any(kw in combined for kw in ("resume", "curriculum vitae", "cover_letter", "cover letter")):
                if field.type == "text":
                    field.type = "file"

        # Detect custom dropzone wrappers that lack an extracted file field
        dropzones = soup.find_all(
            lambda t: isinstance(t, Tag)
            and (
                any(
                    re.search(r"dropzone|_ashby_file|ashby-file-upload", c, re.I)
                    for c in (t.get("class") or [])
                )
                or t.has_attr("data-ashby-dropzone")
                or "dropzone" in (t.get("data-testid") or "").lower()
            )
        )
        for dz in dropzones:
            if dz.find("input", attrs={"type": "file"}):
                continue
            container = self._find_ashby_container(dz) or dz
            dz_id = dz.get("id") or dz.get("data-field") or ""
            dz_name = dz.get("data-name") or dz_id or "_ashby_resume"
            if dz_id and schema.get_field_by_id(dz_id):
                continue
            label = (
                _clean_text(dz.get("aria-label"))
                or _extract_container_question_label(container)
                or "Resume"
            )
            if any(f.type == "file" and _normalize_key(f.label) == _normalize_key(label) for f in schema.fields):
                continue
            schema.fields.append(
                FormField(
                    id=dz_id or dz_name,
                    name=dz_name,
                    label=label,
                    type="file",
                    required=_container_has_required_marker(container, dz),
                    aria_label=dz.get("aria-label") or "",
                )
            )

    def _enrich_standard_ashby_fields(self, soup: BeautifulSoup, schema: FormSchema) -> None:
        """Improve labels, required flags, and types for Ashby text/email/phone/URL/textarea fields."""
        for field in schema.fields:
            el = self._find_soup_element(soup, field)
            container = self._find_ashby_container(el) if el is not None else None

            if container is not None:
                q_label = _extract_container_question_label(container, exclude_tags=("input", "textarea", "select"))
                if q_label and (
                    not field.label
                    or field.label == field.placeholder
                    or field.label.lower().startswith("ashby")
                    or _normalize_key(field.label) == _normalize_key(field.name or field.id)
                ):
                    field.label = q_label
                if _container_has_required_marker(container, el):
                    field.required = True

            if field.type == "text":
                input_mode = (el.get("inputmode") or "").lower() if el is not None else ""
                combined = f"{field.id} {field.name} {field.label}".lower()
                if input_mode == "email" or _ASHBY_EMAIL_HINT_RE.search(combined):
                    field.type = "email"
                elif input_mode == "tel" or _ASHBY_PHONE_HINT_RE.search(combined):
                    field.type = "tel"
                elif input_mode == "url" or _ASHBY_URL_FIELD_RE.search(combined):
                    field.type = "url"

    def _extract_contenteditable_textareas(self, soup: BeautifulSoup, schema: FormSchema) -> None:
        """Capture rich-text contenteditable screening inputs not marked with role=textbox."""
        for el in soup.find_all(attrs={"contenteditable": "true"}):
            if not isinstance(el, Tag):
                continue
            if el.get("role") == "textbox":
                continue
            el_id = el.get("id") or el.get("data-field") or ""
            el_name = el.get("name") or el.get("data-name") or el_id
            if el_id and schema.get_field_by_id(el_id):
                continue
            container = self._find_ashby_container(el)
            label = (
                _clean_text(el.get("aria-label"))
                or _extract_container_question_label(container)
                or el_name
                or "Screening Response"
            )
            schema.fields.append(
                FormField(
                    id=el_id or f"ashby_rich_text_{len(schema.fields)}",
                    name=el_name or el_id,
                    label=label,
                    type="textarea",
                    required=_container_has_required_marker(container, el),
                    value=_clean_text(el.get_text()),
                    aria_label=el.get("aria-label") or "",
                )
            )

    def fill_custom_controls(self, page: Page, answers: dict[str, str]) -> dict[str, bool]:
        """Fill Ashby `_ashby_` inputs, radios, dropdowns/comboboxes, and file dropzones safely.

        Never clicks final submit controls.
        """
        results: dict[str, bool] = {}

        for raw_key, value in answers.items():
            key = raw_key.strip()
            val_clean = str(value).strip()
            if not val_clean:
                continue

            key_lower = key.lower()
            success = False

            # 1. Custom file dropzone / resume / cover letter uploads
            if self._looks_like_file_key(key_lower) or self._has_matching_file_input(page, key):
                success = self._fill_ashby_file_upload(page, key, val_clean)
                if success:
                    results[raw_key] = True
                    continue

            # 2. Ashby radio buttons & boolean question groups
            success = self._fill_ashby_radio_group(page, key, val_clean)
            if success:
                results[raw_key] = True
                continue

            # 3. Ashby select, searchable combobox, & multi-select controls
            success = self._fill_ashby_select_or_combobox(page, key, val_clean)
            if success:
                results[raw_key] = True
                continue

            # 4. Checkbox controls
            success = self._fill_ashby_checkbox(page, key, val_clean)
            if success:
                results[raw_key] = True
                continue

            # 5. Standard _ashby_ text, email, tel, URL, and screening textareas
            success = self._fill_ashby_text_control(page, key, val_clean)
            results[raw_key] = success

        return results

    def _looks_like_file_key(self, key_lower: str) -> bool:
        """Check whether an answer key references resume/CV/document upload."""
        return any(
            kw in key_lower
            for kw in ("resume", "cv", "cover_letter", "cover letter", "attachment", "dropzone")
        )

    def _has_matching_file_input(self, page: Page, key: str) -> bool:
        """Check whether an input[type='file'] directly matches the given id or name."""
        escaped = _escape_css_attr(key)
        try:
            loc = page.locator(f'input[type="file"][id="{escaped}"], input[type="file"][name="{escaped}"]')
            return loc.count() > 0
        except Exception:
            return False

    def _find_container_for_key(self, page: Page, key: str) -> Locator | None:
        """Locate an Ashby field wrapper container matching a key or question label."""
        escaped = _escape_css_attr(key)
        direct_selectors = [
            f'[data-field="{escaped}"]',
            f'[data-name="{escaped}"]',
        ]
        for sel in direct_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    return loc
            except Exception:
                pass

        try:
            pattern = re.compile(re.escape(key), re.IGNORECASE)
            containers = page.locator(
                ".ashby-application-form-field-entry, [class*='_ashby_field' i], "
                "[class*='_ashby_question' i], [class*='_ashby_dropzone' i], "
                "[class*='_ashby_select' i], [class*='_ashby_multiselect' i], "
                "[class*='ashby-field' i], [class*='ashby-question' i], "
                "[class*='ashby-select' i], fieldset, .form-group, .form-field"
            ).filter(has_text=pattern)
            if containers.count() > 0:
                return containers.first
        except Exception:
            pass
        return None

    def _fill_ashby_file_upload(self, page: Page, key: str, file_path_str: str) -> bool:
        """Upload a file into an Ashby file input or custom dropzone wrapper."""
        file_path = Path(file_path_str)
        if not file_path.exists():
            logger.debug("Ashby file upload skipped; path does not exist: '%s'", file_path_str)
            return False

        escaped = _escape_css_attr(key)
        key_lower = key.lower()
        candidates = [
            f'input[type="file"][id="{escaped}"]',
            f'input[type="file"][name="{escaped}"]',
        ]
        if "cover" in key_lower:
            candidates.extend(
                [
                    'input[type="file"][name*="cover" i]',
                    'input[type="file"][id*="cover" i]',
                ]
            )
        else:
            candidates.extend(
                [
                    'input[type="file"][name*="resume" i]',
                    'input[type="file"][id*="resume" i]',
                    'input[type="file"][name*="_ashby_" i]',
                    '[class*="_ashby_dropzone" i] input[type="file"]',
                    '[class*="dropzone" i] input[type="file"]',
                    'input[type="file"]',
                ]
            )

        for sel in candidates:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.set_input_files(str(file_path))
                    return True
            except Exception as exc:
                logger.debug("Ashby file upload selector '%s' failed: %s", sel, exc)

        container = self._find_container_for_key(page, key)
        if container is not None:
            try:
                loc = container.locator('input[type="file"]').first
                if loc.count() > 0:
                    loc.set_input_files(str(file_path))
                    return True
            except Exception:
                pass
        return False

    def _match_option_text(self, target: str, candidate: str) -> bool:
        """Match answer string against an option label or value with boolean aliases."""
        t = _clean_text(target).lower()
        c = _clean_text(candidate).lower()
        if not t or not c:
            return False
        if t == c:
            return True
        if t in ("true", "1", "yes") and c == "yes":
            return True
        if t in ("false", "0", "no") and c == "no":
            return True
        return t in c or c in t

    def _fill_ashby_radio_group(self, page: Page, key: str, value: str) -> bool:
        """Fill native Ashby radio groups or ARIA boolean toggle groups."""
        escaped = _escape_css_attr(key)

        # 1. Native <input type="radio"> matched by name
        try:
            radios = page.locator(
                f'input[type="radio"][name="{escaped}"], input[type="radio"][name*="{escaped}" i]'
            )
            count = radios.count()
            if count > 0:
                if self._select_matching_native_radio(page, radios, count, value):
                    return True
        except Exception as exc:
            logger.debug("Ashby native radio fill error for '%s': %s", key, exc)

        # 2. Enclosing Ashby container or ARIA radiogroup matching key
        container = self._find_container_for_key(page, key)
        if container is None:
            try:
                rg = page.locator('[role="radiogroup"]').filter(
                    has_text=re.compile(re.escape(key), re.IGNORECASE)
                )
                if rg.count() > 0:
                    container = rg.first
            except Exception:
                pass

        if container is not None:
            try:
                radios = container.locator('input[type="radio"]')
                count = radios.count()
                if count > 0 and self._select_matching_native_radio(page, radios, count, value):
                    return True
            except Exception:
                pass

            # Custom ARIA role="radio" or boolean toggle buttons inside group
            try:
                opt_btns = container.locator('[role="radio"], button[type="button"], button:not([type="submit"])')
                btn_count = opt_btns.count()
                for pass_exact in (True, False):
                    for i in range(btn_count):
                        btn = opt_btns.nth(i)
                        text = _clean_text(btn.inner_text() or btn.get_attribute("aria-label") or "")
                        if _is_submit_like(
                            text=text,
                            btn_type=btn.get_attribute("type") or "",
                            elem_id=btn.get_attribute("id") or "",
                            elem_class=btn.get_attribute("class") or "",
                        ):
                            continue
                        if pass_exact:
                            if text.lower() == value.lower():
                                btn.click()
                                return True
                        elif self._match_option_text(value, text):
                            btn.click()
                            return True
            except Exception as exc:
                logger.debug("Ashby custom radio/boolean group error for '%s': %s", key, exc)

        return False

    def _select_matching_native_radio(self, page: Page, radios: Locator, count: int, value: str) -> bool:
        """Check the native radio option matching ``value`` (exact first, then fuzzy)."""
        for pass_exact in (True, False):
            for i in range(count):
                radio = radios.nth(i)
                r_val = radio.get_attribute("value") or ""
                r_id = radio.get_attribute("id") or ""
                r_aria = radio.get_attribute("aria-label") or ""

                lbl_text = ""
                if r_id:
                    lbl_loc = page.locator(f'label[for="{_escape_css_attr(r_id)}"]').first
                    if lbl_loc.count() > 0:
                        lbl_text = lbl_loc.inner_text()
                if not lbl_text:
                    parent_lbl = radio.locator("xpath=ancestor::label[1]")
                    if parent_lbl.count() > 0:
                        lbl_text = parent_lbl.first.inner_text()

                candidates = [c for c in (r_val, lbl_text, r_aria) if _clean_text(c)]
                if pass_exact:
                    matched = any(_clean_text(c).lower() == value.lower() for c in candidates)
                else:
                    matched = any(self._match_option_text(value, c) for c in candidates)

                if matched:
                    radio.check(force=True)
                    return True
        return False

    def _fill_ashby_select_or_combobox(self, page: Page, key: str, value: str) -> bool:
        """Fill native selects, multi-selects, and React combobox/listbox controls."""
        escaped = _escape_css_attr(key)
        container = self._find_container_for_key(page, key)

        # 1. Native <select> (single or multiple)
        select_loc = None
        for sel in (f'select[id="{escaped}"]', f'select[name="{escaped}"]', f'select[aria-label*="{escaped}" i]'):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    select_loc = loc
                    break
            except Exception:
                continue

        if select_loc is None and container is not None:
            try:
                loc = container.locator("select").first
                if loc.count() > 0:
                    select_loc = loc
            except Exception:
                pass

        if select_loc is not None:
            return self._select_native_options(select_loc, value)

        # 2. Custom combobox / dropdown / multi-select control
        combo_loc = None
        combo_selectors = [
            f'[role="combobox"][id="{escaped}"]',
            f'[role="combobox"][name="{escaped}"]',
            f'[id="{escaped}"][aria-haspopup="listbox"]',
            f'[class*="_ashby_select" i][id="{escaped}"]',
            f'[class*="_ashby_multiselect" i][id="{escaped}"]',
            f'[class*="_ashby_combobox" i][id="{escaped}"]',
        ]
        for sel in combo_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    combo_loc = loc
                    break
            except Exception:
                continue

        if combo_loc is None and container is not None:
            try:
                loc = container.locator(
                    '[role="combobox"], [aria-haspopup="listbox"], '
                    '[class*="_ashby_select" i], [class*="_ashby_combobox" i], '
                    '[class*="_ashby_multiselect" i], [class*="ashby-select" i]'
                ).first
                if loc.count() > 0:
                    combo_loc = loc
            except Exception:
                pass

        if combo_loc is None and container is not None:
            try:
                c_classes = (container.get_attribute("class") or "").lower()
                c_role = (container.get_attribute("role") or "").lower()
                c_multi = (container.get_attribute("aria-multiselectable") or "").lower() == "true"
                if (
                    c_role in ("combobox", "listbox")
                    or c_multi
                    or any(
                        tok in c_classes
                        for tok in (
                            "_ashby_select",
                            "_ashby_combobox",
                            "_ashby_multiselect",
                            "ashby-select",
                            "multiselect",
                            "multi-select",
                        )
                    )
                ):
                    combo_loc = container
            except Exception:
                pass

        if combo_loc is None:
            return False

        return self._interact_custom_combobox(page, combo_loc, container, value)

    def _select_native_options(self, select_loc: Locator, value: str) -> bool:
        """Select single or comma-separated multiple options on a native <select>."""
        try:
            is_multiple = select_loc.get_attribute("multiple") is not None
            raw_tokens = [t.strip() for t in value.split(",") if t.strip()] if (is_multiple and "," in value) else [value]

            options = select_loc.locator("option")
            opt_count = options.count()
            resolved_vals: list[str] = []

            for token in raw_tokens:
                found_val: str | None = None
                for pass_exact in (True, False):
                    for i in range(opt_count):
                        opt = options.nth(i)
                        o_val = opt.get_attribute("value") or ""
                        o_text = _clean_text(opt.inner_text())
                        if pass_exact:
                            if o_text.lower() == token.lower() or o_val.lower() == token.lower():
                                found_val = o_val or o_text
                                break
                        elif self._match_option_text(token, o_text) or self._match_option_text(token, o_val):
                            found_val = o_val or o_text
                            break
                    if found_val is not None:
                        break
                if found_val is not None:
                    resolved_vals.append(found_val)

            if resolved_vals:
                select_loc.select_option(resolved_vals if is_multiple else resolved_vals[0])
                return True

            # Direct fallback
            try:
                select_loc.select_option(label=value)
                return True
            except Exception:
                select_loc.select_option(value=value)
                return True
        except Exception as exc:
            logger.debug("Ashby native <select> fill failed: %s", exc)
            return False

    def _interact_custom_combobox(
        self,
        page: Page,
        combo_loc: Locator,
        container: Locator | None,
        value: str,
    ) -> bool:
        """Interact with Ashby custom comboboxes, dropdowns, and multi-select controls."""
        try:
            classes = (combo_loc.get_attribute("class") or "").lower()
            container_classes = (container.get_attribute("class") or "").lower() if container is not None else ""
            is_multi = (
                "multiselect" in classes
                or "multi-select" in classes
                or "multiselect" in container_classes
                or "multi-select" in container_classes
                or (combo_loc.get_attribute("aria-multiselectable") or "").lower() == "true"
                or (combo_loc.get_attribute("data-multi") or "").lower() == "true"
            )
            tokens = [t.strip() for t in value.split(",") if t.strip()] if (is_multi and "," in value) else [value]

            # Checkbox-backed multi-select inside container
            if is_multi and container is not None:
                cbs = container.locator('input[type="checkbox"]')
                if cbs.count() > 0:
                    matched_any = False
                    for token in tokens:
                        for i in range(cbs.count()):
                            cb = cbs.nth(i)
                            cb_id = cb.get_attribute("id") or ""
                            lbl_text = ""
                            if cb_id:
                                lbl = page.locator(f'label[for="{_escape_css_attr(cb_id)}"]').first
                                if lbl.count() > 0:
                                    lbl_text = lbl.inner_text()
                            if not lbl_text:
                                anc = cb.locator("xpath=ancestor::label[1]")
                                if anc.count() > 0:
                                    lbl_text = anc.first.inner_text()
                            if self._match_option_text(token, lbl_text or (cb.get_attribute("value") or "")):
                                cb.check(force=True)
                                matched_any = True
                                break
                    if matched_any:
                        return True

            # Click or type into combobox per token
            controls_id = combo_loc.get_attribute("aria-controls") or combo_loc.get_attribute("aria-owns") or ""
            tag_name = combo_loc.evaluate("el => el.tagName.toLowerCase()")

            any_selected = False
            for token in tokens:
                if tag_name == "input":
                    combo_loc.click()
                    combo_loc.fill(token)
                else:
                    combo_loc.click()
                    inner_search = combo_loc.locator("input").first
                    if inner_search.count() > 0 and inner_search.is_visible():
                        inner_search.fill(token)

                option_loc = self._find_visible_combobox_option(page, container, controls_id, token)
                if option_loc is not None:
                    option_loc.click()
                    any_selected = True
                elif tag_name == "input":
                    combo_loc.press("Enter")
                    any_selected = True

            return any_selected
        except Exception as exc:
            logger.debug("Ashby custom combobox fill failed: %s", exc)
            return False

    def _find_visible_combobox_option(
        self,
        page: Page,
        container: Locator | None,
        controls_id: str,
        token: str,
    ) -> Locator | None:
        """Locate an option element matching ``token`` in the linked listbox or page."""
        search_scopes: list[Locator] = []
        if controls_id:
            for cid in controls_id.split():
                scope = page.locator(f'[id="{_escape_css_attr(cid)}"]')
                if scope.count() > 0:
                    search_scopes.append(scope.first)
        if container is not None:
            search_scopes.append(container)
        search_scopes.append(page.locator("body"))

        option_sel = (
            '[role="option"], [class*="_ashby_option" i], '
            '[class*="ashby-select-option" i], [role="listbox"] li'
        )
        for scope in search_scopes:
            try:
                opts = scope.locator(option_sel)
                count = opts.count()
                for pass_exact in (True, False):
                    for i in range(count):
                        opt = opts.nth(i)
                        text = _clean_text(opt.inner_text() or opt.get_attribute("data-value") or "")
                        if not text:
                            continue
                        if pass_exact:
                            if text.lower() == token.lower():
                                return opt
                        elif self._match_option_text(token, text):
                            return opt
            except Exception:
                continue
        return None

    def _fill_ashby_checkbox(self, page: Page, key: str, value: str) -> bool:
        """Toggle Ashby checkbox controls based on boolean answer value."""
        escaped = _escape_css_attr(key)
        cb_loc = None
        for sel in (f'input[type="checkbox"][id="{escaped}"]', f'input[type="checkbox"][name="{escaped}"]'):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    cb_loc = loc
                    break
            except Exception:
                continue

        if cb_loc is None:
            container = self._find_container_for_key(page, key)
            if container is not None:
                try:
                    loc = container.locator('input[type="checkbox"], [role="checkbox"]').first
                    if loc.count() > 0:
                        cb_loc = loc
                except Exception:
                    pass

        if cb_loc is None:
            return False

        try:
            norm = value.strip().lower()
            truthy = norm in ("true", "1", "yes", "on", "checked", "agree")
            falsy = norm in ("false", "0", "no", "off", "unchecked")
            role = (cb_loc.get_attribute("role") or "").lower()
            if role == "checkbox":
                checked = (cb_loc.get_attribute("aria-checked") or "").lower() == "true"
                if (truthy and not checked) or (falsy and checked) or (not truthy and not falsy and not checked):
                    cb_loc.click()
                return True
            if falsy:
                cb_loc.uncheck(force=True)
            else:
                cb_loc.check(force=True)
            return True
        except Exception as exc:
            logger.debug("Ashby checkbox fill error for '%s': %s", key, exc)
            return False

    def _fill_ashby_text_control(self, page: Page, key: str, value: str) -> bool:
        """Fill Ashby text, email, phone, URL, or screening textarea controls."""
        escaped = _escape_css_attr(key)
        input_selector = (
            'input:not([type="submit"]):not([type="button"]):not([type="reset"])'
            ':not([type="image"]):not([type="hidden"]):not([type="file"])'
            ':not([type="radio"]):not([type="checkbox"]), '
            'textarea, [contenteditable="true"]'
        )
        direct_selectors = [
            f'input[id="{escaped}"]:not([type="submit"]):not([type="button"]):not([type="file"]):not([type="radio"]):not([type="checkbox"])',
            f'input[name="{escaped}"]:not([type="submit"]):not([type="button"]):not([type="file"]):not([type="radio"]):not([type="checkbox"])',
            f'textarea[id="{escaped}"]',
            f'textarea[name="{escaped}"]',
            f'[contenteditable="true"][id="{escaped}"]',
            f'[aria-label="{escaped}"]',
        ]

        target: Locator | None = None
        for sel in direct_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    target = loc
                    break
            except Exception:
                continue

        if target is None:
            container = self._find_container_for_key(page, key)
            if container is not None:
                try:
                    loc = container.locator(input_selector).first
                    if loc.count() > 0:
                        target = loc
                except Exception:
                    pass

        if target is None:
            try:
                lbl_loc = page.get_by_label(key, exact=False).first
                if lbl_loc.count() > 0:
                    tag = lbl_loc.evaluate("e => e.tagName.toLowerCase()")
                    input_type = (lbl_loc.get_attribute("type") or "").lower()
                    is_ce = (lbl_loc.get_attribute("contenteditable") or "").lower() == "true"
                    if (
                        (tag == "input" and input_type not in ("submit", "button", "reset", "image", "hidden", "file", "radio", "checkbox"))
                        or tag == "textarea"
                        or is_ce
                    ):
                        target = lbl_loc
            except Exception:
                pass

        if target is None:
            return False

        try:
            if _is_submit_like(
                text=target.inner_text() if target.evaluate("e => e.tagName.toLowerCase()") == "button" else "",
                btn_type=target.get_attribute("type") or "",
                elem_id=target.get_attribute("id") or "",
                elem_class=target.get_attribute("class") or "",
                aria_label=target.get_attribute("aria-label") or "",
            ):
                logger.warning("Ashby fill blocked on submission-like control '%s'.", key)
                return False

            if (target.get_attribute("contenteditable") or "").lower() == "true":
                target.click()
                target.fill(value)
            else:
                target.fill(value)
            return True
        except Exception as exc:
            logger.debug("Ashby text control fill error for '%s': %s", key, exc)
            return False

    def detect_form_step(self, page_or_html: str | Page) -> tuple[str, bool]:
        """Detect current Ashby form step and whether it is the final submission step."""
        if isinstance(page_or_html, str):
            return self._detect_ashby_step_html(page_or_html)
        return self._detect_ashby_step_page(page_or_html)

    def _detect_ashby_step_html(self, html: str) -> tuple[str, bool]:
        """Detect Ashby step name and final-submit status from raw HTML."""
        soup = BeautifulSoup(html, "html.parser")
        step_name = ""

        step_el = soup.select_one(
            '[aria-current="step"], '
            '[class*="_ashby_step" i][class*="active" i], '
            '[class*="ashby-step" i][class*="active" i], '
            '[data-ashby-step="active"], '
            '[data-step-active="true"], '
            '.ashby-application-form-step-title, '
            '.step.active'
        )
        if step_el:
            step_name = _clean_text(step_el.get_text())

        if not step_name:
            heading = soup.select_one(
                ".ashby-application-form h2, .ashby-application-form h3, "
                "[class*='_ashby_' i] h2, form h2"
            )
            if heading:
                step_name = _clean_text(heading.get_text())

        has_next = False
        has_submit = False

        for btn in soup.find_all(["button", "input", "a"]):
            if btn.has_attr("disabled") or btn.has_attr("hidden"):
                continue
            style = (btn.get("style") or "").lower()
            if re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", style):
                continue
            tag = btn.name.lower()
            btn_type = (btn.get("type") or "").lower()
            if tag == "input" and btn_type not in ("button", "submit"):
                continue

            text = _clean_text(btn.get_text() or btn.get("value") or "")
            elem_id = btn.get("id") or ""
            elem_class = " ".join(btn.get("class") or [])
            aria_label = btn.get("aria-label") or ""
            data_testid = btn.get("data-testid") or ""

            if _is_submit_like(text, btn_type, elem_id, elem_class, aria_label, data_testid):
                has_submit = True
            elif NEXT_BUTTON_PATTERN.search(text) or re.search(
                r"_ashby_next|ashby-next|next-step", f"{elem_id} {elem_class} {data_testid}", re.I
            ):
                has_next = True

        is_review = any(kw in step_name.lower() for kw in ("review", "submit"))
        is_final = is_review or has_submit or not has_next
        if not step_name:
            step_name = "Submit Application" if is_final else "Ashby Application Step"
        return (step_name, is_final)

    def _detect_ashby_step_page(self, page: Page) -> tuple[str, bool]:
        """Detect Ashby step name and final-submit status from a live Playwright Page."""
        step_name = ""
        try:
            step_loc = page.locator(
                '[aria-current="step"], '
                '[class*="_ashby_step" i][class*="active" i], '
                '[class*="ashby-step" i][class*="active" i], '
                '[data-ashby-step="active"], '
                '[data-step-active="true"], '
                '.ashby-application-form-step-title, '
                '.step.active'
            ).first
            if step_loc.count() > 0 and step_loc.is_visible():
                step_name = _clean_text(step_loc.inner_text())
        except Exception:
            pass

        if not step_name:
            try:
                heading = page.locator(
                    ".ashby-application-form h2, .ashby-application-form h3, "
                    "[class*='_ashby_' i] h2, form h2"
                ).first
                if heading.count() > 0 and heading.is_visible():
                    step_name = _clean_text(heading.inner_text())
            except Exception:
                pass

        has_next = False
        has_submit = False

        try:
            buttons = page.locator("button, input[type='button'], input[type='submit'], [role='button']")
            count = buttons.count()
            for i in range(count):
                btn = buttons.nth(i)
                if not btn.is_visible() or not btn.is_enabled():
                    continue
                text = _clean_text(btn.inner_text() or btn.get_attribute("value") or "")
                btn_type = (btn.get_attribute("type") or "").lower()
                elem_id = btn.get_attribute("id") or ""
                elem_class = btn.get_attribute("class") or ""
                aria_label = btn.get_attribute("aria-label") or ""
                data_testid = btn.get_attribute("data-testid") or ""

                if _is_submit_like(text, btn_type, elem_id, elem_class, aria_label, data_testid):
                    has_submit = True
                elif NEXT_BUTTON_PATTERN.search(text) or re.search(
                    r"_ashby_next|ashby-next|next-step", f"{elem_id} {elem_class} {data_testid}", re.I
                ):
                    has_next = True
        except Exception as exc:
            logger.debug("Error inspecting Ashby buttons for step detection: %s", exc)

        is_review = any(kw in step_name.lower() for kw in ("review", "submit"))
        is_final = is_review or has_submit or not has_next
        if not step_name:
            step_name = "Submit Application" if is_final else "Ashby Application Step"
        return (step_name, is_final)

    def advance_step(self, page: Page) -> bool:
        """Advance to the next intermediate Ashby step; NEVER clicks final submit."""
        step_name, is_final = self.detect_form_step(page)
        if is_final:
            logger.warning(
                "advance_step intercepted: Ashby form is on final submission step ('%s'). "
                "Automated submit clicks are strictly blocked for human approval safety.",
                step_name,
            )
            return False

        next_btn = self._find_ashby_next_button(page)
        if next_btn is None:
            logger.info("advance_step: No intermediate next/continue button found on Ashby page.")
            return False

        try:
            btn_text = _clean_text(next_btn.inner_text() or next_btn.get_attribute("value") or "")
            btn_type = (next_btn.get_attribute("type") or "").lower()
            elem_id = next_btn.get_attribute("id") or ""
            elem_class = next_btn.get_attribute("class") or ""
            aria_label = next_btn.get_attribute("aria-label") or ""
            data_testid = next_btn.get_attribute("data-testid") or ""

            if _is_submit_like(btn_text, btn_type, elem_id, elem_class, aria_label, data_testid):
                logger.warning(
                    "advance_step blocked: Button '%s' resembles a final submission control.",
                    btn_text or elem_id,
                )
                return False

            if not next_btn.is_visible() or not next_btn.is_enabled():
                return False

            logger.info("advance_step: Clicking Ashby intermediate navigation button '%s'...", btn_text)
            next_btn.click()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            return True
        except Exception as exc:
            logger.error("advance_step encountered an error clicking Ashby next button: %s", exc)
            return False

    def _find_ashby_next_button(self, page: Page) -> Locator | None:
        """Locate an intermediate Ashby Next/Continue button without selecting submit buttons."""
        selectors = [
            'button[class*="_ashby_next" i]',
            'button[class*="ashby-next" i]',
            'button[data-testid*="next" i]',
            'button[data-testid*="continue" i]',
            'button:has-text("Next")',
            'button:has-text("Continue")',
            'button:has-text("Save & Continue")',
            'button:has-text("Save and Continue")',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible() and loc.is_enabled():
                    text = _clean_text(loc.inner_text() or loc.get_attribute("value") or "")
                    if not _is_submit_like(
                        text=text,
                        btn_type=loc.get_attribute("type") or "",
                        elem_id=loc.get_attribute("id") or "",
                        elem_class=loc.get_attribute("class") or "",
                        aria_label=loc.get_attribute("aria-label") or "",
                        data_testid=loc.get_attribute("data-testid") or "",
                    ):
                        return loc
            except Exception:
                continue

        return self._find_next_button(page)


__all__ = ["AshbyAdapter"]
