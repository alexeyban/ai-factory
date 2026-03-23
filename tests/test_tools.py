"""Unit tests for shared/tools.py local skills."""
import subprocess
import tempfile
from pathlib import Path

import pytest

from shared.tools import (
    ToolResult,
    build_file_tree,
    build_import_map,
    run_git_diff,
    syntax_check,
)


# ---------------------------------------------------------------------------
# syntax_check
# ---------------------------------------------------------------------------


def test_syntax_check_valid_file():
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write("def foo():\n    return 42\n")
        path = Path(f.name)
    try:
        result = syntax_check(path)
        assert result.ok is True
        assert result.data["errors"] == []
        assert result.error == ""
    finally:
        path.unlink(missing_ok=True)


def test_syntax_check_invalid_file():
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write("def foo(:\n    pass\n")  # deliberate SyntaxError
        path = Path(f.name)
    try:
        result = syntax_check(path)
        assert result.ok is False
        assert len(result.data["errors"]) > 0
        err = result.data["errors"][0]
        assert "line" in err
        assert "col" in err
        assert "message" in err
        assert result.error == ""  # tool ran fine; file had syntax errors
    finally:
        path.unlink(missing_ok=True)


def test_syntax_check_missing_file():
    result = syntax_check(Path("/tmp/does_not_exist_ai_factory_xyz.py"))
    assert result.ok is False
    assert result.error != ""


# ---------------------------------------------------------------------------
# build_file_tree
# ---------------------------------------------------------------------------


def test_build_file_tree_returns_py_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("x = 1\n")
        (root / "tests").mkdir()
        (root / "tests" / "test_main.py").write_text("def test_x(): pass\n")
        # .venv should be excluded
        (root / ".venv").mkdir()
        (root / ".venv" / "lib.py").write_text("")

        result = build_file_tree(root)

        assert result.ok is True
        assert "src/main.py" in result.data["files"]
        assert "tests/test_main.py" in result.data["files"]
        assert not any(".venv" in f for f in result.data["files"])
        assert result.data["total"] == 2


def test_build_file_tree_test_files_detected():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "tests").mkdir()
        (root / "tests" / "test_foo.py").write_text("")
        (root / "src.py").write_text("")

        result = build_file_tree(root)

        assert "tests/test_foo.py" in result.data["test_files"]
        assert "src.py" not in result.data["test_files"]


# ---------------------------------------------------------------------------
# build_import_map
# ---------------------------------------------------------------------------


def test_build_import_map_extracts_classes_and_functions():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "mymod.py").write_text(
            "import os\n"
            "class Foo:\n    pass\n"
            "class Bar:\n    pass\n"
            "def calculate(x): return x\n"
            "def _private(): pass\n"
        )

        result = build_import_map(root)

        assert result.ok is True
        mod = result.data["modules"]["mymod.py"]
        assert "Foo" in mod["classes"]
        assert "Bar" in mod["classes"]
        assert "calculate" in mod["functions"]
        assert "_private" not in mod["functions"]


def test_build_import_map_available_imports_format():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "pkg").mkdir()
        (root / "pkg" / "util.py").write_text("class Config: pass\n")

        result = build_import_map(root)

        assert any(
            "from pkg.util import Config" in imp
            for imp in result.data["available_imports"]
        )


def test_build_import_map_excludes_venv_and_tests():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / ".venv").mkdir()
        (root / ".venv" / "site.py").write_text("def venv_func(): pass\n")
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text("class TestX: pass\n")
        (root / "app.py").write_text("def run(): pass\n")

        result = build_import_map(root)

        assert "app.py" in result.data["modules"]
        assert not any(".venv" in k for k in result.data["modules"])
        assert not any(k.startswith("tests/") for k in result.data["modules"])


# ---------------------------------------------------------------------------
# run_git_diff
# ---------------------------------------------------------------------------


def test_run_git_diff_same_branch_is_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        subprocess.run(
            ["git", "init", "-b", "main", str(root)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )
        (root / "f.py").write_text("x = 1\n")
        subprocess.run(
            ["git", "-C", str(root), "add", "."],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )

        result = run_git_diff(root, "main", "main")

        assert result.ok is True
        assert result.data["diff"] == ""
        assert result.data["files_changed"] == 0
