"""RenderCV Typst compilation bridge for JobApplier-Core."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

PDF_MAGIC_HEADER: Final[bytes] = b"%PDF-"
DEFAULT_COMPILE_TIMEOUT_SECONDS: Final[float] = 120.0


class RenderCVNotInstalledError(RuntimeError):
    """Raised when the ``rendercv`` CLI executable cannot be located."""


def ensure_rendercv_installed() -> Path | str:
    """Verify that the ``rendercv`` CLI executable is available.

    Searches ``Path(sys.executable).parent`` (for ``rendercv.exe`` / ``rendercv``)
    alongside ``shutil.which("rendercv")`` so both activated and direct
    virtual-environment Python invocations succeed.

    Returns:
        Resolved path to the ``rendercv`` executable.

    Raises:
        RenderCVNotInstalledError: If no ``rendercv`` executable is found.
    """
    exe_dir = Path(sys.executable).resolve().parent
    candidates: list[Path] = [
        exe_dir / "rendercv.exe",
        exe_dir / "rendercv",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    which_path = shutil.which("rendercv")
    if which_path:
        return Path(which_path)

    raise RenderCVNotInstalledError(
        "RenderCV CLI executable ('rendercv') was not found on PATH or in "
        f"'{exe_dir}'. Install it in the active environment with: "
        'uv pip install "rendercv[full]"'
    )


def _is_valid_pdf(pdf_path: Path) -> bool:
    """Return True if ``pdf_path`` exists, is non-empty, and starts with ``%PDF-``."""
    try:
        if not pdf_path.is_file() or pdf_path.stat().st_size <= len(PDF_MAGIC_HEADER):
            return False
        with pdf_path.open("rb") as handle:
            return handle.read(len(PDF_MAGIC_HEADER)) == PDF_MAGIC_HEADER
    except OSError:
        logger.exception("Failed while validating generated PDF at '%s'.", pdf_path)
        return False


def _locate_compiled_pdf(working_dir: Path, preferred_path: Path) -> Path | None:
    """Locate the PDF produced by RenderCV inside the temporary working directory."""
    if preferred_path.is_file():
        return preferred_path

    candidates = sorted(working_dir.rglob("*.pdf"))
    return candidates[0] if candidates else None


def _log_subprocess_output(stdout: str, stderr: str) -> None:
    """Log non-empty RenderCV stdout/stderr output for diagnostics."""
    if stdout and stdout.strip():
        logger.error("RenderCV stdout:\n%s", stdout.strip())
    if stderr and stderr.strip():
        logger.error("RenderCV stderr:\n%s", stderr.strip())


def compile_resume(
    yaml_path: Path,
    output_pdf_path: Path,
    *,
    timeout: float = DEFAULT_COMPILE_TIMEOUT_SECONDS,
) -> bool:
    """Compile a RenderCV YAML definition into a PDF using Typst.

    Executes ``rendercv render`` inside an isolated temporary working directory
    so intermediate Typst or auxiliary artifacts never clutter the repository,
    then copies the resulting PDF to ``output_pdf_path``.

    Args:
        yaml_path: Path to the input RenderCV YAML file.
        output_pdf_path: Destination path where the compiled PDF should be saved.
        timeout: Subprocess timeout in seconds.

    Returns:
        ``True`` if compilation succeeded and ``output_pdf_path`` is a valid
        non-empty PDF; ``False`` otherwise.
    """
    try:
        rendercv_bin = ensure_rendercv_installed()
    except RenderCVNotInstalledError:
        logger.exception("Cannot compile resume because RenderCV is not installed.")
        return False
    except Exception:
        logger.exception("Unexpected error while checking RenderCV installation.")
        return False

    try:
        source_yaml = Path(yaml_path).resolve()
        target_pdf = Path(output_pdf_path).resolve()

        if not source_yaml.is_file():
            raise FileNotFoundError(f"Input YAML file does not exist: '{source_yaml}'")

        target_pdf.parent.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"

        with tempfile.TemporaryDirectory(prefix="rendercv_compile_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            tmp_output_dir = tmp_dir / "out"
            tmp_pdf_name = "compiled_resume.pdf"
            tmp_pdf_path = tmp_dir / tmp_pdf_name
            tmp_typst_path = tmp_dir / "compiled_resume.typ"

            cmd = [
                str(rendercv_bin),
                "render",
                str(source_yaml),
                "--output-folder",
                str(tmp_output_dir),
                "--typst-path",
                str(tmp_typst_path),
                "--pdf-path",
                str(tmp_pdf_path),
                "--dont-generate-markdown",
                "--dont-generate-html",
                "--dont-generate-png",
            ]

            result = subprocess.run(
                cmd,
                cwd=tmp_dir,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )

            if result.returncode != 0:
                logger.error(
                    "RenderCV failed with exit code %s for '%s'.",
                    result.returncode,
                    source_yaml,
                )
                _log_subprocess_output(result.stdout, result.stderr)
                return False

            compiled_pdf = _locate_compiled_pdf(tmp_dir, tmp_pdf_path)
            if compiled_pdf is None or not _is_valid_pdf(compiled_pdf):
                logger.error(
                    "RenderCV exited with code 0 for '%s', but did not produce a valid PDF.",
                    source_yaml,
                )
                _log_subprocess_output(result.stdout, result.stderr)
                return False

            shutil.copy2(compiled_pdf, target_pdf)

        if not _is_valid_pdf(target_pdf):
            logger.error("Copied PDF at '%s' failed validity checks.", target_pdf)
            return False

        return True
    except subprocess.TimeoutExpired as exc:
        logger.exception("RenderCV timed out after %ss for '%s'.", timeout, yaml_path)
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        _log_subprocess_output(stdout, stderr)
        return False
    except Exception:
        logger.exception("Resume compilation failed for '%s' -> '%s'.", yaml_path, output_pdf_path)
        return False


__all__ = [
    "DEFAULT_COMPILE_TIMEOUT_SECONDS",
    "PDF_MAGIC_HEADER",
    "RenderCVNotInstalledError",
    "compile_resume",
    "ensure_rendercv_installed",
]
