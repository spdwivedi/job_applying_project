"""RenderCV Typst Page-Budget Enforcer for JobApplier-Core.

Enforces target PDF page limits (default: 1 page) on compiled RenderCV v2.8
resumes by inspecting PDF page count via pypdf, progressively tightening
ClassicTheme layout spacing/margins/typography within ATS-readable bounds,
and iteratively trimming the lowest-relevance experience or project bullets
against target job keywords.
"""

from __future__ import annotations

import copy
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pypdf import PdfReader

from src.engine.resume_compiler import compile_resume

logger = logging.getLogger(__name__)

DEFAULT_MAX_TRIM_ITERATIONS = 30

# Absolute unit conversions to typographic points (pt)
_UNIT_TO_PT: dict[str, float] = {
    "pt": 1.0,
    "in": 72.0,
    "cm": 72.0 / 2.54,
    "mm": 72.0 / 25.4,
}

_DIMENSION_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(cm|in|pt|mm|em)\s*$")
_METRIC_RE = re.compile(r"\b\d+(?:[.,]\d+)?(%|x|k|m|b|\+)?\b|\$\d+", re.IGNORECASE)


class PageBudgetResult(BaseModel):
    """Outcome summary from enforcing a PDF page budget on a RenderCV resume."""

    success: bool = Field(..., description="Whether final PDF page count is within target_pages.")
    final_page_count: int = Field(..., description="Page count of the final output PDF.")
    initial_page_count: int = Field(..., description="Page count before any adjustments.")
    adjustments_applied: list[str] = Field(
        default_factory=list,
        description="Ordered list of layout/design tightening adjustments applied.",
    )
    trimmed_bullets: list[str] = Field(
        default_factory=list,
        description="Ordered list of low-relevance highlight bullets removed to fit budget.",
    )
    output_pdf_path: Path = Field(..., description="Destination path of the compiled output PDF.")


# Progressive ClassicTheme tightening tiers within ATS-readable limits
# (floor: 0.4in margins, 9pt body font, 0.42em line spacing).
_DESIGN_TIGHTENING_TIERS: list[tuple[str, dict[str, Any]]] = [
    (
        "Tighten header and section title spacing",
        {
            "header": {
                "space_below_name": "0.45cm",
                "space_below_headline": "0.45cm",
                "space_below_connections": "0.45cm",
            },
            "section_titles": {
                "space_above": "0.35cm",
                "space_below": "0.2cm",
            },
            "sections": {
                "space_between_regular_entries": "0.85em",
                "space_between_text_based_entries": "0.25em",
            },
        },
    ),
    (
        "Tighten page margins to 0.55in and entry spacing",
        {
            "page": {
                "top_margin": "0.55in",
                "bottom_margin": "0.55in",
                "left_margin": "0.55in",
                "right_margin": "0.55in",
            },
            "sections": {
                "space_between_regular_entries": "0.65em",
            },
            "typography": {
                "line_spacing": "0.52em",
            },
        },
    ),
    (
        "Tighten page margins to 0.45in and header/section padding",
        {
            "page": {
                "top_margin": "0.45in",
                "bottom_margin": "0.45in",
                "left_margin": "0.45in",
                "right_margin": "0.45in",
            },
            "header": {
                "space_below_name": "0.3cm",
                "space_below_headline": "0.3cm",
                "space_below_connections": "0.3cm",
            },
            "section_titles": {
                "space_above": "0.25cm",
                "space_below": "0.15cm",
            },
            "sections": {
                "space_between_regular_entries": "0.5em",
                "space_between_text_based_entries": "0.2em",
            },
            "typography": {
                "line_spacing": "0.48em",
            },
        },
    ),
    (
        "Scale typography to 9.5pt body and 0.4in margins",
        {
            "page": {
                "top_margin": "0.4in",
                "bottom_margin": "0.4in",
                "left_margin": "0.4in",
                "right_margin": "0.4in",
            },
            "typography": {
                "line_spacing": "0.45em",
                "font_size": {
                    "body": "9.5pt",
                    "headline": "9.5pt",
                    "connections": "9.5pt",
                    "name": "26pt",
                    "section_titles": "1.3em",
                },
            },
        },
    ),
    (
        "Apply minimum ATS-readable 9pt typography and compact vertical spacing",
        {
            "typography": {
                "line_spacing": "0.42em",
                "font_size": {
                    "body": "9pt",
                    "headline": "9pt",
                    "connections": "9pt",
                    "name": "24pt",
                    "section_titles": "1.25em",
                },
            },
            "header": {
                "space_below_name": "0.25cm",
                "space_below_headline": "0.25cm",
                "space_below_connections": "0.25cm",
            },
            "section_titles": {
                "space_above": "0.2cm",
                "space_below": "0.12cm",
            },
            "sections": {
                "space_between_regular_entries": "0.42em",
                "space_between_text_based_entries": "0.15em",
            },
            "entries": {
                "side_space": "0.1cm",
            },
        },
    ),
]


def get_pdf_page_count(pdf_path: Path | str) -> int:
    """Return the number of pages in a PDF file using ``pypdf.PdfReader``.

    Args:
        pdf_path: Path to the compiled PDF file.

    Returns:
        Integer page count of the PDF.

    Raises:
        FileNotFoundError: If ``pdf_path`` does not exist.
    """
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF file not found: '{path}'")

    reader = PdfReader(str(path))
    return len(reader.pages)


def _extract_design_defaults(model: Any) -> dict[str, Any]:
    """Extract numeric/dimension spacing defaults from a RenderCV theme model without font_family serialization warnings."""
    return {
        "theme": getattr(model, "theme", "classic"),
        "page": {
            "top_margin": model.page.top_margin,
            "bottom_margin": model.page.bottom_margin,
            "left_margin": model.page.left_margin,
            "right_margin": model.page.right_margin,
        },
        "header": {
            "space_below_name": model.header.space_below_name,
            "space_below_headline": model.header.space_below_headline,
            "space_below_connections": model.header.space_below_connections,
        },
        "section_titles": {
            "space_above": model.section_titles.space_above,
            "space_below": model.section_titles.space_below,
        },
        "sections": {
            "space_between_regular_entries": model.sections.space_between_regular_entries,
            "space_between_text_based_entries": model.sections.space_between_text_based_entries,
        },
        "typography": {
            "line_spacing": model.typography.line_spacing,
            "font_size": {
                "body": model.typography.font_size.body,
                "name": model.typography.font_size.name,
                "headline": model.typography.font_size.headline,
                "connections": model.typography.font_size.connections,
                "section_titles": model.typography.font_size.section_titles,
            },
        },
        "entries": {
            "side_space": model.entries.side_space,
        },
    }


def _get_theme_defaults(theme_name: str) -> dict[str, Any]:
    """Retrieve RenderCV v2.8 default design dictionary for a theme."""
    try:
        from rendercv.schema.models.design.built_in_design import built_in_design_adapter

        validated = built_in_design_adapter.validate_python({"theme": theme_name})
        return _extract_design_defaults(validated)
    except Exception:
        try:
            from rendercv.schema.models.design.classic_theme import ClassicTheme

            return _extract_design_defaults(ClassicTheme())
        except Exception:
            return {
                "theme": "classic",
                "page": {
                    "top_margin": "0.7in",
                    "bottom_margin": "0.7in",
                    "left_margin": "0.7in",
                    "right_margin": "0.7in",
                },
                "header": {
                    "space_below_name": "0.7cm",
                    "space_below_headline": "0.7cm",
                    "space_below_connections": "0.7cm",
                },
                "section_titles": {
                    "space_above": "0.5cm",
                    "space_below": "0.3cm",
                },
                "sections": {
                    "space_between_regular_entries": "1.2em",
                    "space_between_text_based_entries": "0.3em",
                },
                "typography": {
                    "line_spacing": "0.6em",
                    "font_size": {
                        "body": "10pt",
                        "name": "30pt",
                        "headline": "10pt",
                        "connections": "10pt",
                        "section_titles": "1.4em",
                    },
                },
                "entries": {
                    "side_space": "0.2cm",
                },
            }


def _dimension_to_pt(dimension: Any, body_pt: float = 10.0) -> float | None:
    """Parse a Typst dimension string (cm, in, pt, mm, em) into typographic points."""
    if not isinstance(dimension, str):
        return None
    match = _DIMENSION_RE.match(dimension)
    if not match:
        return None
    val = float(match.group(1))
    unit = match.group(2)
    if unit == "em":
        return val * body_pt
    return val * _UNIT_TO_PT[unit]


def _resolve_body_pt(design: dict[str, Any], theme_defaults: dict[str, Any]) -> float:
    """Resolve current body font size in points for accurate ``em`` comparisons."""
    typo = design.get("typography")
    if isinstance(typo, dict):
        fs = typo.get("font_size")
        if isinstance(fs, dict) and fs.get("body"):
            parsed = _dimension_to_pt(fs["body"], 10.0)
            if parsed is not None:
                return parsed
    default_body = (
        theme_defaults.get("typography", {}).get("font_size", {}).get("body", "10pt")
    )
    return _dimension_to_pt(default_body, 10.0) or 10.0


def _apply_tighter_overrides(
    design: dict[str, Any],
    overrides: dict[str, Any],
    theme_defaults: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Merge ``overrides`` into ``design`` only where candidate dimensions are strictly smaller."""
    updated = copy.deepcopy(design)
    changed = False
    body_pt = _resolve_body_pt(updated, theme_defaults)

    # Compute target body_pt if this tier also adjusts body font size
    target_body_pt = body_pt
    override_fs = overrides.get("typography", {}).get("font_size", {})
    if isinstance(override_fs, dict) and "body" in override_fs:
        parsed_target = _dimension_to_pt(override_fs["body"], body_pt)
        if parsed_target is not None and parsed_target < body_pt:
            target_body_pt = parsed_target

    def _merge_dict(
        target_node: dict[str, Any],
        override_node: dict[str, Any],
        default_node: dict[str, Any],
    ) -> None:
        nonlocal changed
        for key, candidate_val in override_node.items():
            default_sub = default_node.get(key) if isinstance(default_node, dict) else None
            if isinstance(candidate_val, dict):
                existing_sub = target_node.get(key)
                if not isinstance(existing_sub, dict):
                    existing_sub = {}
                    target_node[key] = existing_sub
                _merge_dict(
                    existing_sub,
                    candidate_val,
                    default_sub if isinstance(default_sub, dict) else {},
                )
            else:
                current_raw = target_node.get(key, default_sub)
                current_pt = _dimension_to_pt(current_raw, body_pt)
                candidate_pt = _dimension_to_pt(candidate_val, target_body_pt)
                if current_pt is None or candidate_pt is None:
                    if target_node.get(key) != candidate_val:
                        target_node[key] = candidate_val
                        changed = True
                elif candidate_pt < current_pt - 1e-6:
                    target_node[key] = candidate_val
                    changed = True

    _merge_dict(updated, overrides, theme_defaults)
    return updated, changed


def _score_bullet_relevance(bullet: str, normalized_keywords: list[str]) -> float:
    """Score a resume highlight bullet against job keywords and quantified impact."""
    text_lower = bullet.lower()
    score = 0.0

    for kw in normalized_keywords:
        pattern = r"(?<!\w)" + re.escape(kw) + r"(?!\w)"
        if re.search(pattern, text_lower):
            score += 3.0
        elif kw in text_lower:
            score += 1.5
        elif " " in kw or "-" in kw:
            subtokens = [t for t in re.split(r"[\s/-]+", kw) if len(t) > 2]
            for token in subtokens:
                if re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", text_lower):
                    score += 0.5

    if _METRIC_RE.search(text_lower):
        score += 0.5

    return score


def _select_lowest_relevance_bullet(
    payload: dict[str, Any],
    normalized_keywords: list[str],
) -> tuple[str, int, int, str] | None:
    """Select the single least relevant highlight bullet to trim next.

    Prioritizes keeping at least one bullet per entry where possible, only
    trimming an entry's final bullet if every entry already has <= 1 bullet
    (while leaving at least one bullet on the resume overall).
    """
    sections = payload.get("cv", {}).get("sections", {})
    if not isinstance(sections, dict):
        return None

    candidate_sections: list[str] = []
    for preferred in ("experience", "projects"):
        if isinstance(sections.get(preferred), list):
            candidate_sections.append(preferred)

    if not candidate_sections:
        for sec_name, sec_items in sections.items():
            if isinstance(sec_items, list) and any(
                isinstance(item, dict) and isinstance(item.get("highlights"), list) and item["highlights"]
                for item in sec_items
            ):
                candidate_sections.append(sec_name)

    surplus_candidates: list[tuple[tuple[float, int, int, int, int], str, int, int, str]] = []
    final_bullet_candidates: list[tuple[tuple[float, int, int, int, int], str, int, int, str]] = []
    total_bullets = 0

    for sec_name in candidate_sections:
        entries = sections.get(sec_name, [])
        if not isinstance(entries, list):
            continue
        section_priority = 0 if sec_name == "projects" else 1

        for entry_idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            highlights = entry.get("highlights")
            if not isinstance(highlights, list) or not highlights:
                continue

            total_bullets += len(highlights)
            for bullet_idx, bullet_raw in enumerate(highlights):
                bullet_text = str(bullet_raw)
                score = _score_bullet_relevance(bullet_text, normalized_keywords)
                sort_key = (
                    score,
                    section_priority,
                    -len(highlights),
                    -entry_idx,
                    -bullet_idx,
                )
                record = (sort_key, sec_name, entry_idx, bullet_idx, bullet_text)
                if len(highlights) > 1:
                    surplus_candidates.append(record)
                else:
                    # Never leave experience[0] empty if it is the sole primary role
                    if not (sec_name == "experience" and entry_idx == 0):
                        final_bullet_candidates.append(record)

    if surplus_candidates:
        surplus_candidates.sort(key=lambda item: item[0])
        _, sec_name, entry_idx, bullet_idx, bullet_text = surplus_candidates[0]
        return (sec_name, entry_idx, bullet_idx, bullet_text)

    if total_bullets > 1 and final_bullet_candidates:
        final_bullet_candidates.sort(key=lambda item: item[0])
        _, sec_name, entry_idx, bullet_idx, bullet_text = final_bullet_candidates[0]
        return (sec_name, entry_idx, bullet_idx, bullet_text)

    return None


def _compile_candidate_payload(
    payload: dict[str, Any],
    work_dir: Path,
    step_label: str,
) -> tuple[bool, int, Path]:
    """Write ``payload`` to a temporary YAML file and compile it to PDF."""
    tmp_yaml = work_dir / f"{step_label}.yaml"
    tmp_pdf = work_dir / f"{step_label}.pdf"
    tmp_yaml.write_text(
        yaml.dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    ok = compile_resume(tmp_yaml, tmp_pdf)
    if not ok or not tmp_pdf.is_file():
        return (False, 0, tmp_pdf)
    try:
        pages = get_pdf_page_count(tmp_pdf)
        return (True, pages, tmp_pdf)
    except Exception:
        logger.exception("Failed to read compiled PDF page count at '%s'.", tmp_pdf)
        return (False, 0, tmp_pdf)


def enforce_page_budget(
    yaml_path: Path | str,
    output_pdf_path: Path | str,
    target_pages: int = 1,
    job_keywords: list[str] | None = None,
    output_yaml_path: Path | str | None = None,
    max_trim_iterations: int = DEFAULT_MAX_TRIM_ITERATIONS,
) -> PageBudgetResult:
    """Compile a RenderCV YAML resume and enforce ``target_pages`` PDF page budget.

    Workflow:
    1. Compile initial YAML and inspect page count via ``pypdf``. If within
       ``target_pages``, save and return immediately.
    2. Progressively tighten ``design`` margins, vertical spacing, and readable
       ATS typography bounds, recompiling until page count fits or tiers exhaust.
    3. If still overflowing, iteratively trim the least relevant experience or
       project highlight bullet against ``job_keywords`` (preserving at least
       1 bullet per entry where possible) until ``page_count <= target_pages``
       or ``max_trim_iterations`` is reached.

    Args:
        yaml_path: Path to input RenderCV YAML file.
        output_pdf_path: Destination path for the final compiled PDF.
        target_pages: Maximum allowed page count (defaults to ``1``).
        job_keywords: Optional list of target role keywords for bullet scoring.
        output_yaml_path: Optional path to write the final adjusted RenderCV YAML.
        max_trim_iterations: Safety cap on iterative bullet removals.

    Returns:
        Structured ``PageBudgetResult`` detailing page counts and modifications.
    """
    if target_pages < 1:
        raise ValueError("target_pages must be >= 1")

    source_yaml = Path(yaml_path)
    target_pdf = Path(output_pdf_path)
    target_yaml = Path(output_yaml_path) if output_yaml_path is not None else None

    if not source_yaml.is_file():
        logger.error("Input RenderCV YAML file does not exist: '%s'", source_yaml)
        return PageBudgetResult(
            success=False,
            final_page_count=0,
            initial_page_count=0,
            adjustments_applied=[],
            trimmed_bullets=[],
            output_pdf_path=target_pdf,
        )

    try:
        raw_payload = yaml.safe_load(source_yaml.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.exception("Failed to parse RenderCV YAML from '%s'.", source_yaml)
        return PageBudgetResult(
            success=False,
            final_page_count=0,
            initial_page_count=0,
            adjustments_applied=[],
            trimmed_bullets=[],
            output_pdf_path=target_pdf,
        )

    if not isinstance(raw_payload, dict) or "cv" not in raw_payload:
        logger.error("Invalid RenderCV YAML structure in '%s': missing 'cv' key.", source_yaml)
        return PageBudgetResult(
            success=False,
            final_page_count=0,
            initial_page_count=0,
            adjustments_applied=[],
            trimmed_bullets=[],
            output_pdf_path=target_pdf,
        )

    payload: dict[str, Any] = copy.deepcopy(raw_payload)
    if not isinstance(payload.get("design"), dict):
        payload["design"] = {"theme": "classic"}

    theme_name = str(payload["design"].get("theme") or "classic")
    theme_defaults = _get_theme_defaults(theme_name)

    adjustments_applied: list[str] = []
    trimmed_bullets: list[str] = []
    normalized_keywords = [kw.strip().lower() for kw in (job_keywords or []) if kw and kw.strip()]

    target_pdf.parent.mkdir(parents=True, exist_ok=True)
    if target_yaml is not None:
        target_yaml.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="page_budget_") as tmp_dir_str:
        work_dir = Path(tmp_dir_str)

        # Step 1: Compile initial YAML directly
        init_pdf = work_dir / "step1_initial.pdf"
        if not compile_resume(source_yaml, init_pdf) or not init_pdf.is_file():
            return PageBudgetResult(
                success=False,
                final_page_count=0,
                initial_page_count=0,
                adjustments_applied=[],
                trimmed_bullets=[],
                output_pdf_path=target_pdf,
            )

        try:
            initial_pages = get_pdf_page_count(init_pdf)
        except Exception:
            logger.exception("Unable to inspect initial PDF page count at '%s'.", init_pdf)
            return PageBudgetResult(
                success=False,
                final_page_count=0,
                initial_page_count=0,
                adjustments_applied=[],
                trimmed_bullets=[],
                output_pdf_path=target_pdf,
            )

        current_pages = initial_pages
        best_pdf = init_pdf

        # Immediate pass if already within budget
        if current_pages <= target_pages:
            shutil.copy2(best_pdf, target_pdf)
            if target_yaml is not None:
                target_yaml.write_text(
                    yaml.dump(payload, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
            return PageBudgetResult(
                success=True,
                final_page_count=current_pages,
                initial_page_count=initial_pages,
                adjustments_applied=[],
                trimmed_bullets=[],
                output_pdf_path=target_pdf,
            )

        # Step 2: Progressive design spacing, margin, and typography tightening
        for tier_idx, (tier_desc, tier_overrides) in enumerate(_DESIGN_TIGHTENING_TIERS, start=1):
            candidate_design, changed = _apply_tighter_overrides(
                payload["design"],
                tier_overrides,
                theme_defaults,
            )
            if not changed:
                continue

            candidate_payload = copy.deepcopy(payload)
            candidate_payload["design"] = candidate_design
            ok, new_pages, candidate_pdf = _compile_candidate_payload(
                candidate_payload,
                work_dir,
                f"step2_tier_{tier_idx}",
            )
            if not ok:
                logger.warning("Compilation failed at design tier '%s'; skipping tier.", tier_desc)
                continue

            payload = candidate_payload
            current_pages = new_pages
            best_pdf = candidate_pdf
            adjustments_applied.append(tier_desc)
            logger.info("Design tier '%s' -> %s page(s).", tier_desc, current_pages)

            if current_pages <= target_pages:
                break

        # Step 3: Iteratively trim lowest-relevance bullets if still overflowing
        trim_count = 0
        while current_pages > target_pages and trim_count < max_trim_iterations:
            selection = _select_lowest_relevance_bullet(payload, normalized_keywords)
            if selection is None:
                logger.info("No remaining trimmable bullets; stopping page-budget enforcement.")
                break

            sec_name, entry_idx, bullet_idx, bullet_text = selection
            candidate_payload = copy.deepcopy(payload)
            entry_dict = candidate_payload["cv"]["sections"][sec_name][entry_idx]
            highlights = entry_dict["highlights"]
            highlights.pop(bullet_idx)
            if not highlights:
                entry_dict.pop("highlights", None)

            trim_count += 1
            ok, new_pages, candidate_pdf = _compile_candidate_payload(
                candidate_payload,
                work_dir,
                f"step3_trim_{trim_count}",
            )
            if not ok:
                logger.warning("Compilation failed after trimming bullet; reverting last trim.")
                break

            payload = candidate_payload
            current_pages = new_pages
            best_pdf = candidate_pdf
            trimmed_bullets.append(bullet_text)
            logger.info(
                "Trimmed bullet from %s[%s] -> %s page(s).",
                sec_name,
                entry_idx,
                current_pages,
            )

        # Persist best compiled PDF and optional final YAML
        shutil.copy2(best_pdf, target_pdf)
        if target_yaml is not None:
            target_yaml.write_text(
                yaml.dump(payload, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )

    return PageBudgetResult(
        success=(current_pages <= target_pages),
        final_page_count=current_pages,
        initial_page_count=initial_pages,
        adjustments_applied=adjustments_applied,
        trimmed_bullets=trimmed_bullets,
        output_pdf_path=target_pdf,
    )


__all__ = [
    "DEFAULT_MAX_TRIM_ITERATIONS",
    "PageBudgetResult",
    "enforce_page_budget",
    "get_pdf_page_count",
]
