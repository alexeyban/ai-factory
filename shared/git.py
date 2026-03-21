import os
import re
import subprocess
from pathlib import Path
from typing import Iterable


DEFAULT_GIT_USER_NAME = os.getenv("AI_FACTORY_GIT_USER_NAME", "AI Factory Bot")
DEFAULT_GIT_USER_EMAIL = os.getenv(
    "AI_FACTORY_GIT_USER_EMAIL", "ai-factory@example.local"
)
DEFAULT_GITHUB_OWNER = os.getenv("GITHUB_OWNER", "alexeyban")

DEFAULT_PROJECT_REPO = os.getenv(
    "DEFAULT_PROJECT_REPO", "git@github.com:alexeyban/reversi-alpha-zero.git"
)
PROJECTS_ROOT = Path("/workspace/projects")


def slugify(value: str, separator: str = "_") -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", separator, value.strip().lower())
    normalized = re.sub(rf"{re.escape(separator)}+", separator, normalized)
    normalized = normalized.strip(separator)
    return normalized or "project"


def run_git(
    repo_path: Path, args: Iterable[str], check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=check,
        capture_output=True,
        text=True,
    )


def ensure_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    if not (repo_path / ".git").exists():
        run_git(repo_path, ["init", "-b", "main"])
    run_git(repo_path, ["config", "user.name", DEFAULT_GIT_USER_NAME])
    run_git(repo_path, ["config", "user.email", DEFAULT_GIT_USER_EMAIL])


def has_commits(repo_path: Path) -> bool:
    ensure_repo(repo_path)
    result = run_git(repo_path, ["rev-parse", "--verify", "HEAD"], check=False)
    return result.returncode == 0


def ensure_branch(repo_path: Path, branch_name: str, base_branch: str = "main") -> None:
    ensure_repo(repo_path)
    run_git(repo_path, ["checkout", base_branch])
    run_git(repo_path, ["checkout", "-B", branch_name, base_branch])


def checkout_branch(repo_path: Path, branch_name: str) -> None:
    ensure_repo(repo_path)
    run_git(repo_path, ["checkout", branch_name])


def branch_exists(repo_path: Path, branch_name: str) -> bool:
    result = run_git(repo_path, ["rev-parse", "--verify", branch_name], check=False)
    return result.returncode == 0


def has_changes(repo_path: Path) -> bool:
    status = run_git(repo_path, ["status", "--porcelain"], check=False)
    return bool(status.stdout.strip())


def commit_all(repo_path: Path, message: str) -> str | None:
    ensure_repo(repo_path)
    run_git(repo_path, ["add", "."])
    if not has_changes(repo_path):
        return None
    run_git(repo_path, ["commit", "-m", message])
    return run_git(repo_path, ["rev-parse", "HEAD"]).stdout.strip()


def merge_branch(
    repo_path: Path, source_branch: str, target_branch: str = "main"
) -> str:
    ensure_repo(repo_path)
    run_git(repo_path, ["checkout", target_branch])
    run_git(
        repo_path,
        [
            "merge",
            "--no-ff",
            source_branch,
            "-m",
            f"Merge {source_branch} into {target_branch}",
        ],
    )
    return run_git(repo_path, ["rev-parse", "HEAD"]).stdout.strip()


def ensure_origin_remote(repo_path: Path, project_name: str) -> str:
    ensure_repo(repo_path)
    repo_slug = slugify(project_name, separator="_")
    remote_url = f"git@github.com:{DEFAULT_GITHUB_OWNER}/{repo_slug}.git"
    remotes = run_git(repo_path, ["remote"], check=False).stdout.split()
    if "origin" in remotes:
        current_url = run_git(
            repo_path, ["remote", "get-url", "origin"], check=False
        ).stdout.strip()
        if current_url != remote_url:
            run_git(repo_path, ["remote", "set-url", "origin", remote_url], check=False)
    else:
        run_git(repo_path, ["remote", "add", "origin", remote_url], check=False)
    return remote_url


def bootstrap_from_remote(
    repo_path: Path, branch_name: str = "main", remote: str = "origin"
) -> bool:
    ensure_repo(repo_path)
    fetch_result = run_git(repo_path, ["fetch", remote, branch_name], check=False)
    if fetch_result.returncode != 0:
        return False

    remote_ref = f"{remote}/{branch_name}"
    if not has_commits(repo_path):
        checkout_result = run_git(
            repo_path,
            ["checkout", "-B", branch_name, "FETCH_HEAD"],
            check=False,
        )
        if checkout_result.returncode != 0:
            return False
    run_git(
        repo_path,
        ["branch", "--set-upstream-to", remote_ref, branch_name],
        check=False,
    )
    return True


def current_branch(repo_path: Path) -> str:
    ensure_repo(repo_path)
    return run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()


def push_branch(
    repo_path: Path, branch_name: str, remote: str = "origin"
) -> dict[str, str | int | bool]:
    result = run_git(
        repo_path,
        ["push", "-u", remote, branch_name],
        check=False,
    )
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def clone_or_pull_project(
    repo_url: str | None = None, project_name: str | None = None, branch: str = "master"
) -> Path:
    """Clone a project repository or pull latest changes if it already exists."""
    repo_url = repo_url or DEFAULT_PROJECT_REPO

    if repo_url.startswith("https://github.com/"):
        if repo_url.endswith(".git"):
            repo_url = repo_url.replace("https://github.com/", "git@github.com:")
        else:
            repo_url = (
                repo_url.replace("https://github.com/", "git@github.com:") + ".git"
            )

    if project_name:
        repo_path = PROJECTS_ROOT / project_name
    else:
        repo_path = PROJECTS_ROOT / Path(repo_url.split("/")[-1].replace(".git", ""))

    repo_path.parent.mkdir(parents=True, exist_ok=True)

    if repo_path.exists() and (repo_path / ".git").exists():
        run_git(repo_path, ["config", "user.name", DEFAULT_GIT_USER_NAME])
        run_git(repo_path, ["config", "user.email", DEFAULT_GIT_USER_EMAIL])
        run_git(repo_path, ["fetch", "origin"])
        run_git(repo_path, ["pull", "origin", branch])
    else:
        subprocess.run(
            ["git", "clone", "--branch", branch, repo_url, str(repo_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        run_git(repo_path, ["config", "user.name", DEFAULT_GIT_USER_NAME])
        run_git(repo_path, ["config", "user.email", DEFAULT_GIT_USER_EMAIL])

    return repo_path


def get_or_create_project_path(
    github_url: str | None = None, project_name: str | None = None
) -> Path:
    """Get or create project path, cloning if necessary."""
    repo_url = github_url or DEFAULT_PROJECT_REPO

    if project_name:
        repo_path = PROJECTS_ROOT / project_name
    else:
        repo_path = PROJECTS_ROOT / Path(repo_url.split("/")[-1].replace(".git", ""))

    if not repo_path.exists():
        return clone_or_pull_project(repo_url, project_name)

    return repo_path
