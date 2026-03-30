"""Local skills for AI Factory agents.

All tools are deterministic, run locally inside Docker containers,
and never make LLM/AI model calls. Each returns a ToolResult dict.
"""
import ast
import json
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from shared.git import run_git

MAX_IMPORTABLE_SYMBOLS = 50


@dataclass
class ToolResult:
    """Uniform return type for all local skills."""

    ok: bool
    output: str  # raw text (stdout+stderr or repr)
    data: dict   # parsed structured data (tool-specific)
    error: str = ""  # non-empty only if the tool itself failed to run


# ---------------------------------------------------------------------------
# 1. Syntax check
# ---------------------------------------------------------------------------


def syntax_check(file_path: Path) -> ToolResult:
    """Parse file_path with ast.parse(). No subprocess — stdlib only.

    data = {errors: [{line, col, message}], summary: str}
    ok=False means the file has syntax errors; QA should not run pytest.
    Non-.py files (e.g. requirements.txt, .md) are skipped with ok=True.
    """
    if file_path.suffix != ".py":
        return ToolResult(
            ok=True,
            output=f"syntax check skipped (not a .py file: {file_path.suffix})",
            data={"errors": [], "summary": "Skipped — not a Python file."},
        )
    try:
        source = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult(
            ok=False,
            output="",
            data={"errors": [], "summary": ""},
            error=f"cannot read file: {exc}",
        )
    try:
        ast.parse(source, filename=str(file_path))
        return ToolResult(
            ok=True,
            output="syntax ok",
            data={"errors": [], "summary": "No syntax errors found."},
        )
    except SyntaxError as exc:
        errors = [{"line": exc.lineno, "col": exc.offset, "message": exc.msg}]
        return ToolResult(
            ok=False,
            output=str(exc),
            data={
                "errors": errors,
                "summary": f"SyntaxError at line {exc.lineno}: {exc.msg}",
            },
        )


# ---------------------------------------------------------------------------
# 2. File tree
# ---------------------------------------------------------------------------


def build_file_tree(repo_path: Path) -> ToolResult:
    """Recursively list .py files, excluding .venv/.

    data = {files: [...], test_files: [...], total: int}
    """
    all_py: list[str] = []
    test_py: list[str] = []
    for p in sorted(repo_path.rglob("*.py")):
        parts = p.relative_to(repo_path).parts
        if ".venv" in parts:
            continue
        rel = str(p.relative_to(repo_path))
        all_py.append(rel)
        if parts[0] == "tests" or p.stem.startswith("test_"):
            test_py.append(rel)
    data = {"files": all_py, "test_files": test_py, "total": len(all_py)}
    return ToolResult(ok=True, output="\n".join(all_py), data=data)


# ---------------------------------------------------------------------------
# 3. Import map
# ---------------------------------------------------------------------------


def build_import_map(repo_path: Path) -> ToolResult:
    """Extract top-level classes and public functions from each .py file.

    Excludes .venv/ and tests/. Caps total available_imports at MAX_IMPORTABLE_SYMBOLS.

    data = {
        modules: {"rel/path.py": {imports, classes, functions}},
        available_imports: ["from module import Symbol", ...]
    }
    """
    modules: dict[str, dict] = {}
    available_imports: list[str] = []
    symbol_count = 0

    for p in sorted(repo_path.rglob("*.py")):
        parts = p.relative_to(repo_path).parts
        if ".venv" in parts or (parts and parts[0] == "tests"):
            continue
        rel = str(p.relative_to(repo_path))
        try:
            source = p.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(p))
        except (OSError, SyntaxError):
            continue

        imports: list[str] = []
        classes: list[str] = []
        functions: list[str] = []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imports.append(alias.asname or alias.name)
            elif isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    functions.append(node.name)

        modules[rel] = {
            "imports": imports,
            "classes": classes,
            "functions": functions,
        }

        module_dotted = rel.replace("/", ".").removesuffix(".py")
        for name in classes + functions:
            if symbol_count >= MAX_IMPORTABLE_SYMBOLS:
                break
            available_imports.append(f"from {module_dotted} import {name}")
            symbol_count += 1

    data = {"modules": modules, "available_imports": available_imports}
    return ToolResult(
        ok=True, output=f"{len(modules)} modules scanned", data=data
    )


# ---------------------------------------------------------------------------
# 4. Lint (ruff)
# ---------------------------------------------------------------------------


def run_lint(file_path: Path, python_path: Path) -> ToolResult:
    """Run ruff check via the project venv. Gracefully skipped if ruff absent.

    data = {issues: [{file, line, col, code, message}], error_count, warning_count}
    ok=False means lint issues were found (not that ruff failed to run).
    error non-empty means ruff was not available or crashed.
    """
    ruff_path = python_path.parent / "ruff"
    if not ruff_path.exists():
        return ToolResult(
            ok=False,
            output="",
            data={"issues": [], "error_count": 0, "warning_count": 0},
            error="ruff not available in venv",
        )
    try:
        proc = subprocess.run(
            [str(ruff_path), "check", "--output-format=json", str(file_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return ToolResult(
            ok=False,
            output="",
            data={"issues": [], "error_count": 0, "warning_count": 0},
            error=str(exc),
        )

    issues: list[dict] = []
    try:
        raw = json.loads(proc.stdout) if proc.stdout.strip() else []
        for item in raw:
            issues.append(
                {
                    "file": item.get("filename", ""),
                    "line": item.get("location", {}).get("row", 0),
                    "col": item.get("location", {}).get("column", 0),
                    "code": item.get("code", ""),
                    "message": item.get("message", ""),
                }
            )
    except json.JSONDecodeError:
        pass

    error_count = sum(1 for i in issues if not i["code"].startswith("W"))
    warning_count = len(issues) - error_count
    ok = proc.returncode == 0
    data = {
        "issues": issues,
        "error_count": error_count,
        "warning_count": warning_count,
    }
    return ToolResult(ok=ok, output=proc.stdout + proc.stderr, data=data)


# ---------------------------------------------------------------------------
# 5. Type check (mypy)
# ---------------------------------------------------------------------------


def run_typecheck(
    file_path: Path, repo_path: Path, python_path: Path
) -> ToolResult:
    """Run mypy --output json. Gracefully skipped if mypy absent.

    data = {errors: [{file, line, message, severity}], error_count}
    ok=False means type errors found (not that mypy failed to run).
    error non-empty means mypy was not available or crashed.
    """
    mypy_path = python_path.parent / "mypy"
    if not mypy_path.exists():
        return ToolResult(
            ok=False,
            output="",
            data={"errors": [], "error_count": 0},
            error="mypy not available in venv",
        )
    try:
        proc = subprocess.run(
            [
                str(mypy_path),
                "--no-error-summary",
                "--output",
                "json",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(repo_path),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return ToolResult(
            ok=False,
            output="",
            data={"errors": [], "error_count": 0},
            error=str(exc),
        )

    errors: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            errors.append(
                {
                    "file": obj.get("file", ""),
                    "line": obj.get("line", 0),
                    "message": obj.get("message", ""),
                    "severity": obj.get("severity", "error"),
                }
            )
        except json.JSONDecodeError:
            continue  # skip non-JSON header lines

    ok = proc.returncode == 0
    data = {"errors": errors, "error_count": len(errors)}
    return ToolResult(ok=ok, output=proc.stdout + proc.stderr, data=data)


# ---------------------------------------------------------------------------
# 6. Pytest with coverage
# ---------------------------------------------------------------------------


def parse_junit_xml(junit_path: Path) -> dict:
    """
    Parse a pytest junit XML file and return pass/fail/total counts.

    Returns dict with keys: tests_passed, tests_failed, tests_total, tests_errored.
    Returns all-zeros if the file is missing or unparseable.
    """
    result = {"tests_passed": 0, "tests_failed": 0, "tests_total": 0, "tests_errored": 0}
    if not junit_path.exists():
        return result
    try:
        tree = ET.parse(str(junit_path))
        root = tree.getroot()
        # junit XML has a <testsuite> root or is wrapped in <testsuites>
        suites = root.findall(".//testsuite") or [root]
        for suite in suites:
            total = int(suite.get("tests", 0))
            failures = int(suite.get("failures", 0))
            errors = int(suite.get("errors", 0))
            skipped = int(suite.get("skipped", 0))
            passed = total - failures - errors - skipped
            result["tests_total"] += total
            result["tests_failed"] += failures
            result["tests_errored"] += errors
            result["tests_passed"] += max(0, passed)
    except ET.ParseError:
        pass
    return result


def run_pytest_with_coverage(
    repo_path: Path,
    python_path: Path,
    timeout: int,
    module_name: str,
) -> ToolResult:
    """Run pytest with --cov and --junit-xml. Falls back gracefully if plugins missing.

    data = {returncode, stdout, stderr,
            coverage: {percent, covered_lines, total_lines, missing_lines} | None,
            junit: {tests_passed, tests_failed, tests_total, tests_errored}}
    """
    cov_report_file = Path(tempfile.mktemp(suffix=".json"))
    junit_report_file = Path(tempfile.mktemp(suffix=".xml"))
    cmd = [
        str(python_path),
        "-m",
        "pytest",
        str(repo_path),
        "-v",
        f"--cov={module_name}",
        f"--cov-report=json:{cov_report_file}",
        f"--junit-xml={junit_report_file}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(30, timeout),
            cwd=str(repo_path),
        )
    except subprocess.TimeoutExpired as exc:
        return ToolResult(
            ok=False,
            output=f"pytest timed out after {timeout}s",
            data={
                "returncode": -1,
                "stdout": "",
                "stderr": str(exc),
                "coverage": None,
                "junit": {"tests_passed": 0, "tests_failed": 0,
                          "tests_total": 0, "tests_errored": 0},
            },
            error="timeout",
        )

    coverage: Optional[dict] = None
    if cov_report_file.exists():
        try:
            raw = json.loads(cov_report_file.read_text())
            totals = raw.get("totals", {})
            coverage = {
                "percent": totals.get("percent_covered", 0.0),
                "covered_lines": totals.get("covered_lines", 0),
                "total_lines": totals.get("num_statements", 0),
                "missing_lines": totals.get("missing_lines", []),
            }
        except (json.JSONDecodeError, KeyError):
            coverage = None
        try:
            cov_report_file.unlink()
        except OSError:
            pass

    junit = parse_junit_xml(junit_report_file)
    try:
        junit_report_file.unlink(missing_ok=True)
    except OSError:
        pass

    ok = proc.returncode == 0
    data = {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "coverage": coverage,
        "junit": junit,
    }
    return ToolResult(ok=ok, output=proc.stdout + proc.stderr, data=data)


# ---------------------------------------------------------------------------
# 7. Previous error history
# ---------------------------------------------------------------------------


def get_task_error_history(repo_path: Path, task_id: str) -> ToolResult:
    """Read previous attempt errors and QA feedback from the task state file.

    Returns a human-readable summary of what went wrong on each prior attempt
    so the dev agent can try a different approach.

    data = {
        attempts: [{attempt, error, qa_status, qa_summary, approach_tried}],
        total_failures: int,
        last_error: str,
    }
    """
    state_path = repo_path / ".ai_factory" / "tasks" / f"{task_id}.json"
    if not state_path.exists():
        return ToolResult(
            ok=True,
            output="No previous attempts recorded.",
            data={"attempts": [], "total_failures": 0, "last_error": ""},
        )

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ToolResult(
            ok=False,
            output="",
            data={"attempts": [], "total_failures": 0, "last_error": ""},
            error=f"cannot read task state: {exc}",
        )

    attempts: list[dict] = []
    # task state uses "healing_history" list of {attempt, dev, qa} entries
    history = state.get("healing_history", state.get("attempts", []))
    # Flat state (single attempt recorded directly)
    if not history and state.get("last_qa_result"):
        history = [{
            "attempt": state.get("attempts", 1),
            "qa": state.get("last_qa_result", {}),
            "dev": state.get("last_dev_result", {}),
        }]

    for entry in history:
        attempt_num = entry.get("attempt", entry.get("attempt_number", "?"))
        dev = entry.get("dev", {}) or {}
        qa = entry.get("qa", {}) or {}
        error = dev.get("error", entry.get("error", ""))
        qa_status = qa.get("status", entry.get("status", ""))
        # QA summary may be a dict (from _summarize_qa_result) or a string
        qa_summary_raw = qa.get("summary", entry.get("qa_summary", ""))
        if isinstance(qa_summary_raw, dict):
            qa_summary = (
                qa_summary_raw.get("error_summary", "")
                or qa_summary_raw.get("root_cause", "")
                or str(qa_summary_raw)
            )[:500]
        else:
            qa_summary = str(qa_summary_raw)[:500]
        # logs give extra context (first 300 chars)
        qa_logs_snippet = str(qa.get("logs", ""))[:300]
        attempts.append({
            "attempt": attempt_num,
            "error": str(error)[:300] if error else "",
            "qa_status": qa_status,
            "qa_summary": qa_summary,
            "qa_logs_snippet": qa_logs_snippet,
            "approach_tried": dev.get("mode", ""),
        })

    failures = [a for a in attempts if a["qa_status"] not in ("success", "complete", "")]
    last_error = failures[-1]["error"] or failures[-1]["qa_summary"] if failures else ""

    lines = []
    for a in attempts:
        lines.append(f"Attempt {a['attempt']}: status={a['qa_status']}")
        if a["error"]:
            lines.append(f"  Error: {a['error']}")
        if a["qa_summary"]:
            lines.append(f"  QA summary: {a['qa_summary']}")
        if a["qa_logs_snippet"]:
            lines.append(f"  Log excerpt: {a['qa_logs_snippet']}")

    return ToolResult(
        ok=True,
        output="\n".join(lines) if lines else "No previous failures.",
        data={
            "attempts": attempts,
            "total_failures": len(failures),
            "last_error": last_error,
        },
    )


# ---------------------------------------------------------------------------
# 8. Git diff
# ---------------------------------------------------------------------------


def run_git_diff(
    repo_path: Path, base_branch: str, target_branch: str
) -> ToolResult:
    """Show git diff between two branches.

    data = {stat, diff, files_changed, insertions, deletions}
    """
    ref = f"{base_branch}..{target_branch}"
    try:
        stat_result = run_git(repo_path, ["diff", ref, "--stat"], check=False)
        diff_result = run_git(repo_path, ["diff", ref], check=False)
    except Exception as exc:
        return ToolResult(ok=False, output="", data={}, error=str(exc))

    stat_text = stat_result.stdout.strip()
    diff_text = diff_result.stdout.strip()

    files_changed = insertions = deletions = 0
    m = re.search(r"(\d+) file", stat_text)
    if m:
        files_changed = int(m.group(1))
    m = re.search(r"(\d+) insertion", stat_text)
    if m:
        insertions = int(m.group(1))
    m = re.search(r"(\d+) deletion", stat_text)
    if m:
        deletions = int(m.group(1))

    data = {
        "stat": stat_text,
        "diff": diff_text,
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
    }
    ok = stat_result.returncode == 0 and diff_result.returncode == 0
    return ToolResult(ok=ok, output=stat_text, data=data)
