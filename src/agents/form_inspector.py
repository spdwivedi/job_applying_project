"""Playwright Form Inspector Agent for JobApplier-Core.

Inspects live ATS job application pages using Playwright, safely parsing
interactive form elements into structured FormSchema definitions without submitting.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

from src.engine.form_parser import (
    FormSchema,
    extract_form_schema_from_html,
    extract_form_schema_from_page,
)

logger = logging.getLogger(__name__)


class FormInspectorAgent:
    """Agent that inspects ATS application forms via Playwright DOM parsing.

    Features:
    - Safe inspection: never activates submit buttons or alters remote state.
    - Persistent auth: supports loading pre-authenticated browser session state.
    - Full coverage: extracts text inputs, textareas, selects, radios, checkboxes, and file uploads.
    """

    def __init__(self, storage_state_path: Path | str | None = None) -> None:
        """Initialize FormInspectorAgent.

        Args:
            storage_state_path: Optional path to a Playwright storage_state.json file.
                If provided and the file exists, session cookies are loaded for persistent login.
        """
        if storage_state_path is not None:
            self.storage_state_path: Path | None = Path(storage_state_path)
        else:
            env_state = os.getenv("STORAGE_STATE_PATH")
            self.storage_state_path = Path(env_state) if env_state else None

    def _get_context_options(self) -> dict[str, Any]:
        """Construct Playwright browser context options, attaching storage_state if present."""
        options: dict[str, Any] = {}
        if self.storage_state_path and self.storage_state_path.is_file():
            options["storage_state"] = str(self.storage_state_path)
        return options

    def inspect_page(self, page: Page) -> FormSchema:
        """Inspect active Playwright page without clicking submit buttons.

        Extracts interactive form controls into FormSchema.

        Args:
            page: Live Playwright Page instance.

        Returns:
            Extracted FormSchema.
        """
        return extract_form_schema_from_page(page)

    def inspect_url(self, url: str, headless: bool = True) -> FormSchema:
        """Launch Playwright Chromium, navigate to URL, call inspect_page, and close browser.

        Args:
            url: Target job application URL.
            headless: Whether to run Chromium in headless mode. Defaults to True.

        Returns:
            Extracted FormSchema.
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            try:
                context = browser.new_context(**self._get_context_options())
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as exc:
                    logger.warning("Navigation notice for %s: %s", url, exc)
                return self.inspect_page(page)
            finally:
                browser.close()

    def inspect_html(self, html_content: str) -> FormSchema:
        """Helper for tests and offline forms (page.set_content(html_content)).

        Args:
            html_content: Raw HTML markup of an application form.

        Returns:
            Extracted FormSchema.
        """
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(**self._get_context_options())
                    page = context.new_page()
                    page.set_content(html_content, wait_until="domcontentloaded")
                    return self.inspect_page(page)
                finally:
                    browser.close()
        except Exception as exc:
            logger.warning("Playwright inspect_html fallback to static parser: %s", exc)
            return extract_form_schema_from_html(html_content)
