"""
Skill Executor — sandboxed execution of extracted skill code.

Execution model:
  - Skill code + a thin harness are written to a temp file
  - Run via subprocess with a hard timeout
  - stdout is interpreted as the result (plain text or JSON)
  - Secrets and environment variables are NOT forwarded to the subprocess

This is an MVP sandbox: isolation is provided by timeout only.
Network sandboxing and full process isolation are left for Phase 8.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memory.skill import Skill

LOGGER = logging.getLogger(__name__)

_HARNESS_TEMPLATE = """\
import json as _json
import sys as _sys

# --- injected skill code ---
{skill_code}
# --- end skill code ---

# Harness: call skill_main if defined, else do nothing
_inputs = _json.loads({inputs_json!r})
if "skill_main" in dir():
    _result = skill_main(**_inputs)
    print(_json.dumps({{"success": True, "output": str(_result)}}))
else:
    print(_json.dumps({{"success": True, "output": "skill loaded"}}))
"""


@dataclass
class SkillResult:
    success: bool
    output: str | None
    error: str | None
    execution_time_ms: float


class SkillExecutor:
    """
    Execute a Skill in an isolated subprocess with a hard timeout.

    Parameters
    ----------
    timeout_sec:
        Maximum wall-clock seconds the subprocess may run (default 10).
    """

    def __init__(self, timeout_sec: int = 10) -> None:
        self._timeout = timeout_sec

    def execute(self, skill: Skill, inputs: dict[str, Any] | None = None) -> SkillResult:
        """
        Execute skill code with the given inputs dict.

        Returns SkillResult with success=False on timeout or Python error.
        Never raises.
        """
        inputs = inputs or {}
        code_path = Path(skill.code_path)

        # Prefer the file on disk; fall back to embedding a placeholder
        if code_path.exists():
            skill_code = code_path.read_text(encoding="utf-8")
        else:
            return SkillResult(
                success=False,
                output=None,
                error=f"Skill file not found: {skill.code_path}",
                execution_time_ms=0.0,
            )

        harness = _HARNESS_TEMPLATE.format(
            skill_code=skill_code,
            inputs_json=json.dumps(inputs),
        )

        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(harness)
            tmp_path = tmp.name

        start = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                # Do not forward the current environment (no secrets)
                env={
                    "PATH": "/usr/bin:/bin",
                    "PYTHONPATH": "",
                },
            )
            elapsed_ms = (time.monotonic() - start) * 1000

            if proc.returncode != 0:
                return SkillResult(
                    success=False,
                    output=proc.stdout.strip() or None,
                    error=proc.stderr.strip() or f"exit code {proc.returncode}",
                    execution_time_ms=elapsed_ms,
                )

            stdout = proc.stdout.strip()
            try:
                data = json.loads(stdout)
                return SkillResult(
                    success=data.get("success", True),
                    output=data.get("output"),
                    error=data.get("error"),
                    execution_time_ms=elapsed_ms,
                )
            except (json.JSONDecodeError, TypeError):
                return SkillResult(
                    success=True,
                    output=stdout,
                    error=None,
                    execution_time_ms=elapsed_ms,
                )

        except subprocess.TimeoutExpired:
            elapsed_ms = (time.monotonic() - start) * 1000
            LOGGER.warning(
                "[skill_executor] Timeout after %ds for skill %s",
                self._timeout, skill.id,
            )
            return SkillResult(
                success=False,
                output=None,
                error=f"Execution timed out after {self._timeout}s",
                execution_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            LOGGER.warning("[skill_executor] Unexpected error: %s", exc)
            return SkillResult(
                success=False,
                output=None,
                error=str(exc),
                execution_time_ms=elapsed_ms,
            )
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
