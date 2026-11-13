"""Playwright Form Inspector and DOM Parser for JobApplier-Core.

Extracts structured FormSchema and FormField objects from live pages or HTML snippets
using accessibility trees, ARIA attributes, semantic tags, and heuristic fallbacks.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from playwright.sync_api import Page


class FormField(BaseModel):
    """Represents an interactive field in an application form."""

    id: str = Field(default="", description="DOM id of the input control.")
    name: str = Field(default="", description="DOM name attribute of the input control.")
    label: str = Field(default="", description="Human-readable label or question text.")
    type: str = Field(
        default="text",
        description="Field control type (text, email, tel, textarea, select, radio, checkbox, file, etc.).",
    )
    required: bool = Field(default=False, description="Whether the field is marked as required.")
    options: list[str] = Field(
        default_factory=list,
        description="List of available option labels/values for select, radio, or multi-choice fields.",
    )
    value: str = Field(default="", description="Current or prefilled value of the field.")
    placeholder: str = Field(default="", description="Placeholder text if present.")
    aria_label: str = Field(default="", description="Aria label if present.")


class FormSchema(BaseModel):
    """Schema containing all extracted form fields from a job application page."""

    fields: list[FormField] = Field(default_factory=list, description="Extracted interactive form fields.")

    def get_field_by_id(self, field_id: str) -> FormField | None:
        """Find a field by id or name attribute.

        Args:
            field_id: The element id or name to search for.

        Returns:
            The matching FormField if found, otherwise None.
        """
        if not field_id:
            return None
        for field in self.fields:
            if field.id == field_id:
                return field
        for field in self.fields:
            if field.name == field_id:
                return field
        return None

    def get_resume_upload_field(self) -> FormField | None:
        """Find the file upload field for Resume or CV.

        Heuristics:
        1. Checks for file type fields with 'resume', 'cv', or 'curriculum' in label, name, or id.
        2. Checks for file type fields with 'attachment', 'document', or 'upload' in label, name, or id.
        3. Returns the first file type field if present.

        Returns:
            The matching FormField for resume upload, or None.
        """
        file_fields = [f for f in self.fields if f.type.lower() == "file"]
        if not file_fields:
            return None

        primary_keywords = ("resume", "cv", "curriculum")
        for f in file_fields:
            searchable = f"{f.label} {f.name} {f.id}".lower()
            if any(kw in searchable for kw in primary_keywords):
                return f

        secondary_keywords = ("attachment", "document", "upload", "attach")
        for f in file_fields:
            searchable = f"{f.label} {f.name} {f.id}".lower()
            if any(kw in searchable for kw in secondary_keywords):
                return f

        return file_fields[0]

    def to_dict(self) -> dict[str, Any]:
        """Convert FormSchema to standard Python dictionary."""
        return self.model_dump()


DOM_EXTRACTION_SCRIPT = """
() => {
    const fields = [];
    const seenElements = new Set();
    const radioGroups = new Map();

    function cleanText(text) {
        if (!text) return "";
        return text.replace(/\\s+/g, ' ').replace(/[\\*\\u2731\\u2217]/g, '').trim();
    }

    function getLabel(el) {
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel && cleanText(ariaLabel)) {
            return cleanText(ariaLabel);
        }

        const ariaLabelledBy = el.getAttribute('aria-labelledby');
        if (ariaLabelledBy) {
            const labels = ariaLabelledBy.split(/\\s+/)
                .map(id => document.getElementById(id))
                .filter(Boolean)
                .map(target => target.innerText || target.textContent || '');
            const combined = cleanText(labels.join(' '));
            if (combined) return combined;
        }

        if (el.labels && el.labels.length > 0) {
            const labelTexts = Array.from(el.labels)
                .map(l => l.innerText || l.textContent || '')
                .filter(Boolean);
            const combined = cleanText(labelTexts.join(' '));
            if (combined) return combined;
        }

        if (el.id) {
            try {
                const labelFor = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                if (labelFor) {
                    const text = cleanText(labelFor.innerText || labelFor.textContent);
                    if (text) return text;
                }
            } catch (e) {}
        }

        const parentLabel = el.closest('label');
        if (parentLabel) {
            const clone = parentLabel.cloneNode(true);
            const nested = clone.querySelectorAll('input, select, textarea');
            nested.forEach(i => i.remove());
            const text = cleanText(clone.innerText || clone.textContent);
            if (text) return text;
        }

        const container = el.closest('.form-group, .form-field, .field, .input-group, .question, [data-automation-id], .application-field, .jobs-form-group, fieldset, tr, td, div');
        if (container) {
            const legend = container.querySelector('legend');
            if (legend) {
                const text = cleanText(legend.innerText || legend.textContent);
                if (text) return text;
            }
            const containerLabel = container.querySelector('label, [class*="label"], [class*="title"], [class*="prompt"], [class*="question"]');
            if (containerLabel && containerLabel !== el && !containerLabel.contains(el)) {
                const text = cleanText(containerLabel.innerText || containerLabel.textContent);
                if (text) return text;
            }
        }

        let prev = el.previousElementSibling;
        while (prev) {
            if (['LABEL', 'SPAN', 'P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6'].includes(prev.tagName)) {
                const text = cleanText(prev.innerText || prev.textContent);
                if (text) return text;
            }
            prev = prev.previousElementSibling;
        }

        const placeholder = el.getAttribute('placeholder');
        if (placeholder && cleanText(placeholder)) {
            return cleanText(placeholder);
        }

        const title = el.getAttribute('title');
        if (title && cleanText(title)) {
            return cleanText(title);
        }

        const fallbackName = el.name || el.id || "";
        if (fallbackName) {
            return fallbackName
                .replace(/[_-]+/g, ' ')
                .replace(/([a-z])([A-Z])/g, '$1 $2')
                .replace(/\\b\\w/g, c => c.toUpperCase())
                .trim();
        }

        return "";
    }

    function isRequired(el, rawText) {
        if (el.required || el.hasAttribute('required')) return true;
        if (el.getAttribute('aria-required') === 'true') return true;
        if (rawText && /[\\*\\u2731\\u2217]|required/i.test(rawText)) return true;
        const container = el.closest('.form-group, .form-field, .field, .input-group, div');
        if (container) {
            const reqMarker = container.querySelector('.required, [class*="asterisk"], [aria-required="true"]');
            if (reqMarker) return true;
        }
        return false;
    }

    const selector = 'input:not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]):not([type="hidden"]), textarea, select, [role="textbox"], [role="combobox"], [role="listbox"], [role="checkbox"], [role="radio"]';
    const elements = Array.from(document.querySelectorAll(selector));

    let fieldIndex = 0;
    for (const el of elements) {
        if (seenElements.has(el)) continue;

        const tagName = el.tagName.toLowerCase();
        let fieldType = "text";
        if (tagName === "textarea") {
            fieldType = "textarea";
        } else if (tagName === "select") {
            fieldType = "select";
        } else if (tagName === "input") {
            fieldType = (el.type || "text").toLowerCase();
        } else {
            const role = (el.getAttribute("role") || "").toLowerCase();
            if (role === "combobox" || role === "listbox") fieldType = "select";
            else if (role === "checkbox") fieldType = "checkbox";
            else if (role === "radio") fieldType = "radio";
            else if (role === "textbox") {
                fieldType = el.getAttribute("aria-multiline") === "true" ? "textarea" : "text";
            }
        }

        const rawLabelText = (el.labels && el.labels[0] ? el.labels[0].innerText : "") || el.getAttribute("aria-label") || "";
        const label = getLabel(el);
        const required = isRequired(el, rawLabelText);
        const name = el.name || el.getAttribute("name") || "";
        let id = el.id || "";
        if (!id) {
            id = name || `field_${fieldIndex}`;
        }
        fieldIndex++;

        let value = el.value || el.getAttribute("value") || "";
        if (!value && tagName !== "input" && tagName !== "select") {
            value = (el.innerText || el.textContent || "").trim();
        }
        const placeholder = el.getAttribute("placeholder") || "";
        const ariaLabel = el.getAttribute("aria-label") || "";

        if (fieldType === "radio" && name) {
            if (!radioGroups.has(name)) {
                let groupLabel = "";
                const fieldset = el.closest('fieldset');
                if (fieldset) {
                    const legend = fieldset.querySelector('legend');
                    if (legend) groupLabel = cleanText(legend.innerText || legend.textContent);
                }
                if (!groupLabel) {
                    const groupContainer = el.closest('.form-group, .form-field, .question, div');
                    if (groupContainer) {
                        const lbl = groupContainer.querySelector('label, [class*="label"], [class*="title"], [class*="question"]');
                        if (lbl && lbl !== el && !lbl.contains(el)) {
                            groupLabel = cleanText(lbl.innerText || lbl.textContent);
                        }
                    }
                }
                if (!groupLabel) groupLabel = label;

                radioGroups.set(name, {
                    id: id,
                    name: name,
                    label: groupLabel || name,
                    type: "radio",
                    required: required,
                    options: [],
                    value: "",
                    placeholder: "",
                    aria_label: ariaLabel
                });
            }
            const group = radioGroups.get(name);
            const optVal = label || value || el.id;
            if (optVal && !group.options.includes(optVal)) {
                group.options.push(optVal);
            }
            if (el.checked) {
                group.value = optVal;
            }
            if (required) group.required = true;
            seenElements.add(el);
            continue;
        }

        let options = [];
        if (fieldType === "select") {
            if (tagName === "select") {
                options = Array.from(el.querySelectorAll('option'))
                    .map(o => (o.text || o.innerText || o.value || '').trim())
                    .filter(t => t.length > 0);
            } else {
                const optionNodes = el.querySelectorAll('[role="option"]');
                if (optionNodes.length > 0) {
                    options = Array.from(optionNodes)
                        .map(o => (o.innerText || o.textContent || o.getAttribute('data-value') || '').trim())
                        .filter(t => t.length > 0);
                }
            }
        }

        seenElements.add(el);
        fields.push({
            id: id,
            name: name,
            label: label,
            type: fieldType,
            required: required,
            options: options,
            value: value,
            placeholder: placeholder,
            aria_label: ariaLabel
        });
    }

    for (const group of radioGroups.values()) {
        fields.push(group);
    }

    return fields;
}
"""


def extract_form_schema_from_page(page: Page) -> FormSchema:
    """Execute DOM inspection script on a live Playwright page and return FormSchema.

    Args:
        page: Live Playwright Page instance.

    Returns:
        Structured FormSchema object.
    """
    raw_fields: list[dict[str, Any]] = page.evaluate(DOM_EXTRACTION_SCRIPT)
    form_fields: list[FormField] = []
    for item in raw_fields:
        form_fields.append(
            FormField(
                id=item.get("id", ""),
                name=item.get("name", ""),
                label=item.get("label", ""),
                type=item.get("type", "text"),
                required=item.get("required", False),
                options=item.get("options", []),
                value=item.get("value", ""),
                placeholder=item.get("placeholder", ""),
                aria_label=item.get("aria_label", ""),
            )
        )
    return FormSchema(fields=form_fields)


def _clean_text(text: str | None) -> str:
    """Normalize whitespace and strip required asterisk markers."""
    if not text:
        return ""
    cleaned = re.sub(r"[\*\u2731\u2217]", "", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_form_schema_from_html(html_content: str) -> FormSchema:
    """Extract FormSchema from raw HTML string using BeautifulSoup heuristics.

    Provides an offline/test alternative matching the Playwright DOM heuristics.

    Args:
        html_content: Raw HTML text.

    Returns:
        Structured FormSchema object.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    fields: list[FormField] = []
    seen_elements: set[Tag] = set()
    radio_groups: dict[str, dict[str, Any]] = {}

    candidate_tags = soup.find_all(
        lambda tag: tag.name in ("input", "textarea", "select")
        or tag.get("role") in ("textbox", "combobox", "listbox", "checkbox", "radio")
    )

    field_index = 0
    for el in candidate_tags:
        if not isinstance(el, Tag) or el in seen_elements:
            continue

        tag_name = el.name.lower()
        input_type = (el.get("type") or "text").lower() if tag_name == "input" else ""

        # Filter out non-fillable inputs
        if tag_name == "input" and input_type in ("submit", "button", "reset", "image", "hidden"):
            continue

        field_type = "text"
        if tag_name == "textarea":
            field_type = "textarea"
        elif tag_name == "select":
            field_type = "select"
        elif tag_name == "input":
            field_type = input_type
        else:
            role = (el.get("role") or "").lower()
            if role in ("combobox", "listbox"):
                field_type = "select"
            elif role == "checkbox":
                field_type = "checkbox"
            elif role == "radio":
                field_type = "radio"
            elif role == "textbox":
                field_type = "textarea" if el.get("aria-multiline") == "true" else "text"

        el_id = el.get("id") or ""
        el_name = el.get("name") or ""
        placeholder = el.get("placeholder") or ""
        aria_label = el.get("aria-label") or ""

        # Label resolution
        label = ""
        if aria_label and _clean_text(aria_label):
            label = _clean_text(aria_label)
        elif el.get("aria-labelledby"):
            ref_ids = str(el.get("aria-labelledby")).split()
            texts = [soup.find(id=rid).get_text() for rid in ref_ids if soup.find(id=rid)]
            if texts:
                label = _clean_text(" ".join(texts))
        elif el_id:
            label_tag = soup.find("label", attrs={"for": el_id})
            if label_tag:
                label = _clean_text(label_tag.get_text())

        if not label:
            parent_label = el.find_parent("label")
            if parent_label:
                clone = BeautifulSoup(str(parent_label), "html.parser").find("label")
                if clone:
                    for sub in clone.find_all(["input", "select", "textarea"]):
                        sub.decompose()
                    label = _clean_text(clone.get_text())

        if not label:
            parent_container = el.find_parent(
                class_=re.compile(r"form-group|form-field|field|input-group|question", re.I)
            ) or el.find_parent("fieldset")
            if parent_container:
                legend = parent_container.find("legend")
                if legend:
                    label = _clean_text(legend.get_text())
                else:
                    lbl = parent_container.find("label")
                    if lbl and lbl != el:
                        label = _clean_text(lbl.get_text())

        if not label:
            prev = el.find_previous_sibling(["label", "span", "p"])
            if prev:
                label = _clean_text(prev.get_text())

        if not label and placeholder:
            label = _clean_text(placeholder)

        if not label and (el_name or el_id):
            raw_id = el_name or el_id
            humanized = re.sub(r"[_-]+", " ", raw_id)
            humanized = re.sub(r"([a-z])([A-Z])", r"\1 \2", humanized).title()
            label = humanized.strip()

        # Required detection
        raw_context = (el.find_parent() or el).get_text() if el.find_parent() else ""
        required = (
            el.has_attr("required")
            or el.get("aria-required") == "true"
            or bool(re.search(r"[\*\u2731\u2217]|required", raw_context, re.I))
        )

        resolved_id = el_id or el_name or f"field_{field_index}"
        field_index += 1

        val = el.get("value") or ""
        if not val and tag_name not in ("input", "select"):
            val = el.get_text().strip()

        # Radio button grouping
        if field_type == "radio" and el_name:
            if el_name not in radio_groups:
                fieldset = el.find_parent("fieldset")
                group_label = ""
                if fieldset:
                    legend = fieldset.find("legend")
                    if legend:
                        group_label = _clean_text(legend.get_text())
                if not group_label:
                    group_label = label or el_name

                radio_groups[el_name] = {
                    "id": resolved_id,
                    "name": el_name,
                    "label": group_label,
                    "type": "radio",
                    "required": required,
                    "options": [],
                    "value": "",
                    "placeholder": "",
                    "aria_label": aria_label,
                }

            group = radio_groups[el_name]
            opt_label = label or val or resolved_id
            if opt_label and opt_label not in group["options"]:
                group["options"].append(opt_label)
            if el.has_attr("checked"):
                group["value"] = opt_label
            if required:
                group["required"] = True

            seen_elements.add(el)
            continue

        # Options for select
        options: list[str] = []
        if field_type == "select":
            if tag_name == "select":
                for opt in el.find_all("option"):
                    opt_text = opt.get_text().strip() or opt.get("value", "").strip()
                    if opt_text:
                        options.append(opt_text)
            else:
                for opt in el.find_all(attrs={"role": "option"}):
                    opt_text = opt.get_text().strip() or opt.get("data-value", "").strip()
                    if opt_text:
                        options.append(opt_text)

        seen_elements.add(el)
        fields.append(
            FormField(
                id=resolved_id,
                name=el_name,
                label=label,
                type=field_type,
                required=required,
                options=options,
                value=val,
                placeholder=placeholder,
                aria_label=aria_label,
            )
        )

    for group_data in radio_groups.values():
        fields.append(
            FormField(
                id=group_data["id"],
                name=group_data["name"],
                label=group_data["label"],
                type=group_data["type"],
                required=group_data["required"],
                options=group_data["options"],
                value=group_data["value"],
                placeholder=group_data["placeholder"],
                aria_label=group_data["aria_label"],
            )
        )

    return FormSchema(fields=fields)
