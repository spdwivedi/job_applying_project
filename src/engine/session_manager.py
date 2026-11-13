"""Stealth Browser & Cookie Session Manager for JobApplier-Core.

Provides robust session state persistence, stealth Playwright context creation,
and headed manual login orchestration for job application platforms.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Final, List, Optional, Set, Tuple, Union

try:
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    Browser = Any  # type: ignore
    BrowserContext = Any  # type: ignore
    Page = Any  # type: ignore
    Playwright = Any  # type: ignore

logger = logging.getLogger(__name__)

SUPPORTED_PORTALS: Final[tuple[str, ...]] = (
    "greenhouse",
    "lever",
    "workday",
    "linkedin",
    "indeed",
)

DEFAULT_PORTALS: Final[tuple[str, ...]] = SUPPORTED_PORTALS

DEFAULT_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_VIEWPORT: Final[dict[str, int]] = {
    "width": 1920,
    "height": 1080,
}

DEFAULT_LOCALE: Final[str] = "en-US"
DEFAULT_TIMEZONE: Final[str] = "America/New_York"
DEFAULT_PERMISSIONS: Final[tuple[str, ...]] = ("geolocation",)

DEFAULT_CHROMIUM_ARGS: Final[tuple[str, ...]] = (
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
)

STEALTH_INIT_SCRIPT: Final[str] = """
(() => {
    // 1. Override navigator.webdriver
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => false,
            configurable: true,
        });
    } catch (e) {}

    // 2. Realistic languages mock
    try {
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
            configurable: true,
        });
    } catch (e) {}

    // 3. Realistic plugins mock
    try {
        const mockPlugins = [
            {
                name: 'Chrome PDF Plugin',
                filename: 'internal-pdf-viewer',
                description: 'Portable Document Format',
                0: { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
            },
            {
                name: 'Chrome PDF Viewer',
                filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                description: 'Chrome PDF Viewer',
                0: { type: 'application/pdf', suffixes: 'pdf', description: 'Chrome PDF Viewer' }
            },
            {
                name: 'Native Client',
                filename: 'internal-nacl-plugin',
                description: 'Native Client Executable',
                0: { type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable' }
            }
        ];
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const arr = [...mockPlugins];
                arr.item = (i) => arr[i] || null;
                arr.namedItem = (name) => arr.find((p) => p.name === name) || null;
                arr.refresh = () => {};
                return arr;
            },
            configurable: true,
        });
    } catch (e) {}

    // 4. Mock window.chrome runtime
    try {
        if (!window.chrome) {
            window.chrome = {
                app: {
                    isInstalled: false,
                    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                    RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
                },
                runtime: {
                    OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
                    OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
                    PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                    PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                    PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
                    RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' }
                }
            };
        }
    } catch (e) {}

    // 5. Hide automation flags & CDP artifacts
    try {
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
    } catch (e) {}

    // 6. Realistic permissions query override
    try {
        if (navigator.permissions && navigator.permissions.query) {
            const origQuery = navigator.permissions.query;
            navigator.permissions.query = (parameters) =>
                parameters && parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : origQuery(parameters);
        }
    } catch (e) {}
})();
"""


def get_storage_state_dir(base_dir: Path | str | None = None) -> Path:
    """Resolve base directory for storing platform session state JSON files."""
    if base_dir is not None:
        return Path(base_dir)
    env_dir = os.getenv("STORAGE_STATE_DIR")
    if env_dir:
        return Path(env_dir)
    return Path("vault") / "platform_cookies"


def get_session_path(portal: str, base_dir: Path | str | None = None) -> Path:
    """Resolve storageState JSON path for a specified portal name."""
    clean_portal = portal.strip().lower()
    return get_storage_state_dir(base_dir) / f"{clean_portal}_storageState.json"


def is_valid_session_file(file_path: Path | str | None) -> bool:
    """Validate that session file exists, is non-empty, and contains valid JSON storage state."""
    if file_path is None:
        return False
    path = Path(file_path)
    if not path.is_file():
        return False
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return False
        data = json.loads(content)
        if not isinstance(data, dict):
            return False
        if "cookies" not in data and "origins" not in data:
            return False
        cookies = data.get("cookies", [])
        origins = data.get("origins", [])
        if not isinstance(cookies, list) or not isinstance(origins, list):
            return False
        # Each cookie entry in Playwright storageState must specify either url or (domain and path)
        for cookie in cookies:
            if not isinstance(cookie, dict):
                return False
            has_domain_path = "domain" in cookie and "path" in cookie
            has_url = "url" in cookie
            if not (has_domain_path or has_url):
                return False
        return True
    except Exception:
        return False


def list_available_sessions(base_dir: Path | str | None = None) -> dict[str, Path]:
    """Detect available saved and valid session states across supported and discovered portals."""
    target_dir = get_storage_state_dir(base_dir)
    available: dict[str, Path] = {}
    if not target_dir.exists():
        return available

    # Check known supported portals
    for portal in SUPPORTED_PORTALS:
        session_file = get_session_path(portal, base_dir=target_dir)
        if is_valid_session_file(session_file):
            available[portal] = session_file

    # Also scan directory for any other custom {portal}_storageState.json files
    try:
        suffix = "_storageState.json"
        for file in target_dir.glob(f"*{suffix}"):
            portal_name = file.name[:-len(suffix)].lower()
            if portal_name not in available and is_valid_session_file(file):
                available[portal_name] = file
    except Exception:
        pass

    return available


def has_session(portal: str, base_dir: Path | str | None = None) -> bool:
    """Check whether a valid session state file exists for the given portal."""
    session_file = get_session_path(portal, base_dir=base_dir)
    return is_valid_session_file(session_file)


def resolve_storage_state_path(
    portal: str | None = None,
    storage_state_path: Path | str | None = None,
    base_dir: Path | str | None = None,
) -> Path | None:
    """Resolve and validate an existing storage state file path."""
    if storage_state_path is not None:
        target = Path(storage_state_path)
        if is_valid_session_file(target):
            return target
        logger.warning("Provided storage_state_path does not exist or is invalid JSON: %s", storage_state_path)
        return None

    if portal:
        candidate = get_session_path(portal, base_dir=base_dir)
        if is_valid_session_file(candidate):
            return candidate

    return None


def create_stealth_context(
    playwright: Playwright,
    portal: str | None = None,
    headless: bool = True,
    storage_state_path: Path | str | None = None,
    **extra_options: Any,
) -> BrowserContext:
    """Configure and return a Playwright BrowserContext with stealth settings and session state."""
    # Resolve valid storage state if exists
    valid_storage_path = resolve_storage_state_path(
        portal=portal,
        storage_state_path=storage_state_path,
        base_dir=extra_options.pop("base_dir", None),
    )

    # Pop browser instance if passed by caller
    browser: Optional[Browser] = extra_options.pop("browser", None)

    if browser is None:
        if hasattr(playwright, "new_context"):
            browser = playwright  # Caller passed Browser directly
        elif hasattr(playwright, "chromium"):
            launch_args = list(extra_options.pop("launch_args", DEFAULT_CHROMIUM_ARGS))
            browser_keys = {"channel", "executable_path", "slow_mo", "timeout"}
            browser_kwargs = {k: extra_options.pop(k) for k in list(extra_options.keys()) if k in browser_keys}
            browser = playwright.chromium.launch(
                headless=headless,
                args=launch_args,
                **browser_kwargs,
            )
        else:
            raise TypeError("Expected Playwright or Browser instance for context creation")

    # Construct context parameters
    context_options: dict[str, Any] = {
        "user_agent": extra_options.pop("user_agent", DEFAULT_USER_AGENT),
        "viewport": extra_options.pop("viewport", dict(DEFAULT_VIEWPORT)),
        "locale": extra_options.pop("locale", DEFAULT_LOCALE),
        "timezone_id": extra_options.pop("timezone_id", DEFAULT_TIMEZONE),
        "permissions": extra_options.pop("permissions", list(DEFAULT_PERMISSIONS)),
    }

    if valid_storage_path is not None:
        context_options["storage_state"] = str(valid_storage_path)
        logger.info("Injected storage state for portal '%s' from %s", portal or "custom", valid_storage_path)

    # Merge remaining options (e.g. bypass_csp, color_scheme, ignore_https_errors)
    context_options.update(extra_options)

    try:
        context = browser.new_context(**context_options)
    except Exception as ctx_err:
        if "storage_state" in context_options:
            logger.warning(
                "Failed to initialize context with storage state (%s). Falling back to clean context.",
                ctx_err,
            )
            context_options.pop("storage_state", None)
            context = browser.new_context(**context_options)
        else:
            raise

    # Inject stealth scripts to override automation flags
    context.add_init_script(STEALTH_INIT_SCRIPT)

    return context


def export_session_state(
    context: BrowserContext,
    portal: str,
    output_path: Path | str | None = None,
) -> Path:
    """Export current cookies and origins from BrowserContext to storageState JSON file."""
    if output_path is not None:
        target_path = Path(output_path)
    else:
        target_path = get_session_path(portal)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(target_path))
    logger.info("Successfully exported session state for portal '%s' to '%s'", portal, target_path)
    return target_path


def launch_manual_login_session(
    playwright: Playwright,
    login_url: str,
    portal: str,
    output_path: Path | str | None = None,
    timeout_seconds: float = 300.0,
    headless: bool = False,
    completion_predicate: Optional[Callable[[Page], bool]] = None,
    **extra_options: Any,
) -> Path:
    """Launch headed Chromium for user authentication, wait for completion, and export storage state."""
    if output_path is not None:
        target_path = Path(output_path)
    else:
        target_path = get_session_path(portal)

    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Launch headed (or headless if explicitly requested by caller/test runner) stealth context
    context = create_stealth_context(
        playwright=playwright,
        portal=portal,
        headless=headless,
        storage_state_path=target_path if is_valid_session_file(target_path) else None,
        **extra_options,
    )

    page = context.new_page()

    def _safe_export() -> None:
        try:
            context.storage_state(path=str(target_path))
        except Exception:
            pass

    page.on("framenavigated", lambda _: _safe_export())
    page.on("close", lambda _: _safe_export())

    logger.info("Launching manual login session for portal '%s' at: %s", portal, login_url)
    try:
        page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
    except Exception as nav_err:
        logger.warning("Initial navigation to login URL '%s' noted: %s", login_url, nav_err)

    start_time = time.time()
    initial_url = page.url

    portal_auth_cookies = {
        "linkedin": {"li_at"},
        "greenhouse": {"_harvest_session", "session", "user_credentials"},
        "lever": {"lever-session", "connect.sid"},
        "indeed": {"CTK", "SHS", "SURF", "IND_AUTH"},
        "workday": {"PLAY_SESSION", "wday_vsession", "workday_session"},
    }
    expected_cookies = portal_auth_cookies.get(portal.strip().lower(), set())

    while (time.time() - start_time) < timeout_seconds:
        if page.is_closed() or not context.pages:
            logger.info("Manual login browser page was closed by the user.")
            break

        if completion_predicate is not None:
            try:
                if completion_predicate(page):
                    logger.info("Manual login completion predicate returned True.")
                    break
            except Exception:
                pass

        if expected_cookies:
            try:
                cookie_names = {c.get("name", "") for c in context.cookies()}
                if expected_cookies.intersection(cookie_names):
                    logger.info("Detected authenticated cookie marker for portal '%s'.", portal)
                    try:
                        page.wait_for_timeout(1000)
                    except Exception:
                        pass
                    break
            except Exception:
                pass

        try:
            current_url = page.url
            if current_url and current_url != initial_url:
                lower_url = current_url.lower()
                if any(kw in lower_url for kw in ["/feed", "/dashboard", "/home", "/jobs", "/profile", "/manage", "/candidates", "/overview"]):
                    logger.info("Detected navigation to authenticated destination: %s", current_url)
                    try:
                        page.wait_for_timeout(1000)
                    except Exception:
                        pass
                    break
        except Exception:
            pass

        try:
            page.wait_for_timeout(500)
        except Exception:
            break

    # Save final snapshot
    try:
        export_session_state(context, portal=portal, output_path=target_path)
    except Exception as exp_err:
        logger.warning("Final export encountered: %s", exp_err)

    # Clean up browser and context
    browser = context.browser
    try:
        context.close()
    except Exception:
        pass

    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass

    return target_path


class SessionManager:
    """High-level manager for browser persistence, stealth contexts, and session state lifecycles."""

    def __init__(self, storage_dir: Path | str | None = None) -> None:
        self.storage_dir: Path = get_storage_state_dir(storage_dir)

    def get_session_path(self, portal: str) -> Path:
        """Resolve storageState path for a portal under this manager's storage directory."""
        return get_session_path(portal, base_dir=self.storage_dir)

    def is_session_valid(self, portal: str) -> bool:
        """Check if portal has a valid existing session state."""
        return is_valid_session_file(self.get_session_path(portal))

    def list_available_sessions(self) -> dict[str, Path]:
        """List all valid saved session state paths."""
        return list_available_sessions(base_dir=self.storage_dir)

    def has_session(self, portal: str) -> bool:
        """Check whether session exists for portal."""
        return self.is_session_valid(portal)

    def create_stealth_context(
        self,
        playwright: Playwright,
        portal: str | None = None,
        headless: bool = True,
        storage_state_path: Path | str | None = None,
        **extra_options: Any,
    ) -> BrowserContext:
        """Create stealth Playwright context using manager's storage state resolution."""
        path = storage_state_path
        if path is None and portal is not None:
            candidate = self.get_session_path(portal)
            if is_valid_session_file(candidate):
                path = candidate
        return create_stealth_context(
            playwright=playwright,
            portal=portal,
            headless=headless,
            storage_state_path=path,
            **extra_options,
        )

    def export_session_state(
        self,
        context: BrowserContext,
        portal: str,
        output_path: Path | str | None = None,
    ) -> Path:
        """Export session state to designated path or manager default portal path."""
        target = Path(output_path) if output_path is not None else self.get_session_path(portal)
        return export_session_state(context=context, portal=portal, output_path=target)

    def launch_manual_login_session(
        self,
        playwright: Playwright,
        login_url: str,
        portal: str,
        output_path: Path | str | None = None,
        timeout_seconds: float = 300.0,
        headless: bool = False,
        completion_predicate: Optional[Callable[[Page], bool]] = None,
        **extra_options: Any,
    ) -> Path:
        """Launch headed manual login session and export session state."""
        target = Path(output_path) if output_path is not None else self.get_session_path(portal)
        return launch_manual_login_session(
            playwright=playwright,
            login_url=login_url,
            portal=portal,
            output_path=target,
            timeout_seconds=timeout_seconds,
            headless=headless,
            completion_predicate=completion_predicate,
            **extra_options,
        )
