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


def _build_ssh_command() -> str:
    """Return a GIT_SSH_COMMAND for git operations.

    When SSH_AUTH_SOCK is set the agent holds the decrypted key — do NOT
    pass -i, which would force SSH to decrypt the file and prompt for a
    passphrase (no TTY in container).  Fall back to an explicit identity
    file only when no agent socket is available.
    """
    cmd = os.getenv("GIT_SSH_COMMAND", "ssh -o StrictHostKeyChecking=no")
    # Agent socket available → let the agent provide the key, no -i needed
    if os.getenv("SSH_AUTH_SOCK"):
        return cmd
    # No agent → try to find an unencrypted (or usable) key file
    if "-i " not in cmd and "IdentityFile" not in cmd:
        for key_path in (
            "/root/.ssh/id_ed25519",
            "/root/.ssh/id_rsa",
            "/root/.ssh/id_ecdsa",
        ):
            if os.path.exists(key_path):
                cmd = f"{cmd} -i {key_path}"
                break
    return cmd


def run_git(
    repo_path: Path, args: Iterable[str], check: bool = True
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = _build_ssh_command()
    # Prevent git from prompting for credentials interactively (no TTY in containers)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def ensure_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    if not (repo_path / ".git").exists():
        run_git(repo_path, ["init", "-b", "main"])
    # check=False: these are idempotent config writes; concurrent tasks on the same
    # repo can race on .git/config and return exit 255 — safe to ignore.
    run_git(repo_path, ["config", "user.name", DEFAULT_GIT_USER_NAME], check=False)
    run_git(repo_path, ["config", "user.email", DEFAULT_GIT_USER_EMAIL], check=False)


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
    repo_slug = slugify(project_name, separator="-")
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
    else:
        # Sync local branch to remote — remote is authoritative (merge commits from PRs
        # may have advanced it since the last local commit).
        run_git(repo_path, ["checkout", branch_name], check=False)
        run_git(repo_path, ["reset", "--hard", "FETCH_HEAD"], check=False)
    run_git(
        repo_path,
        ["branch", "--set-upstream-to", remote_ref, branch_name],
        check=False,
    )
    return True


def current_branch(repo_path: Path) -> str:
    ensure_repo(repo_path)
    return run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()


def _github_api_token() -> str | None:
    token = os.getenv("GITHUB_TOKEN")
    if (
        token
        and len(token) >= 20
        and " " not in token
        and not token.lower().startswith("your")
        and not token.lower().startswith("ghp_your")
    ):
        return token
    return None


def _github_repo_slug(repo_path: Path) -> tuple[str, str] | None:
    result = run_git(repo_path, ["remote", "get-url", "origin"], check=False)
    url = result.stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", url)
    if m:
        return m.group(1), m.group(2)
    return None


def _enable_github_auto_merge(
    api: str,
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    merge_method: str = "MERGE",
) -> dict:
    """Enable auto-merge on a PR via GitHub GraphQL API.

    Used as fallback when branch protection rules block a direct merge.
    Returns {"ok": bool, "error": str|None}.
    """
    import requests

    # First fetch the PR node_id needed for the GraphQL mutation
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    node_resp = requests.get(
        f"{api}/pulls/{pr_number}",
        headers=headers,
        timeout=30,
    )
    if not node_resp.ok:
        return {"ok": False, "error": f"could not fetch PR node_id: {node_resp.text[:200]}"}
    node_id = node_resp.json().get("node_id")
    if not node_id:
        return {"ok": False, "error": "PR node_id missing from response"}

    mutation = """
    mutation($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) {
      enablePullRequestAutoMerge(input: {
        pullRequestId: $pullRequestId,
        mergeMethod: $mergeMethod
      }) {
        pullRequest { autoMergeRequest { mergeMethod } }
      }
    }
    """
    gql_resp = requests.post(
        "https://api.github.com/graphql",
        json={
            "query": mutation,
            "variables": {"pullRequestId": node_id, "mergeMethod": merge_method},
        },
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    if not gql_resp.ok:
        return {"ok": False, "error": f"GraphQL request failed: {gql_resp.text[:200]}"}
    data = gql_resp.json()
    if data.get("errors"):
        return {"ok": False, "error": str(data["errors"])[:300]}
    return {"ok": True, "error": None}


def create_and_merge_github_pr(
    repo_path: Path,
    branch_name: str,
    title: str,
    body: str = "",
    base: str = "main",
) -> dict:
    """Create a GitHub PR for branch_name → base, merge it, and delete the branch.

    Returns {"ok": bool, "pr_url": str|None, "merge_commit": str|None,
             "auto_merge": bool, "error": str|None}.
    - ok=True + auto_merge=False: PR was merged immediately.
    - ok=True + auto_merge=True: direct merge was blocked by branch protection;
      PR has been set to auto-merge when required checks pass.
    - ok=False: PR could not be created or merged.
    Returns ok=False (no exception) when no token or not a GitHub remote.
    """
    import logging
    import requests

    _log = logging.getLogger(__name__)

    token = _github_api_token()
    if not token:
        return {"ok": False, "pr_url": None, "merge_commit": None, "auto_merge": False, "error": "no valid GITHUB_TOKEN"}

    slug = _github_repo_slug(repo_path)
    if not slug:
        return {"ok": False, "pr_url": None, "merge_commit": None, "auto_merge": False, "error": "remote is not github.com"}

    owner, repo = slug
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api = f"https://api.github.com/repos/{owner}/{repo}"

    # Create PR (or find existing open one)
    pr_resp = requests.post(
        f"{api}/pulls",
        json={"title": title, "body": body, "head": branch_name, "base": base},
        headers=headers,
        timeout=30,
    )
    if pr_resp.status_code not in (200, 201):
        list_resp = requests.get(
            f"{api}/pulls",
            params={"head": f"{owner}:{branch_name}", "base": base, "state": "open"},
            headers=headers,
            timeout=30,
        )
        prs = list_resp.json() if list_resp.ok else []
        if not prs:
            return {"ok": False, "pr_url": None, "merge_commit": None, "auto_merge": False, "error": pr_resp.text[:400]}
        pr = prs[0]
    else:
        pr = pr_resp.json()

    pr_number = pr["number"]
    pr_url = pr["html_url"]

    # Merge PR
    merge_resp = requests.put(
        f"{api}/pulls/{pr_number}/merge",
        json={"merge_method": "merge", "commit_title": title},
        headers=headers,
        timeout=30,
    )
    if not merge_resp.ok:
        status_code = merge_resp.status_code
        if status_code in (405, 422):
            _log.warning(
                "[git] PR #%d merge blocked by branch protection rules (HTTP %d). "
                "Required status checks or review approvals may be pending. "
                "Attempting to enable auto-merge so PR merges when checks pass. "
                "PR URL: %s",
                pr_number, status_code, pr_url,
            )
            auto_result = _enable_github_auto_merge(api, token, owner, repo, pr_number)
            if auto_result["ok"]:
                _log.info(
                    "[git] Auto-merge enabled for PR #%d (%s). "
                    "It will merge automatically when all required checks pass.",
                    pr_number, pr_url,
                )
                return {
                    "ok": True,
                    "pr_url": pr_url,
                    "merge_commit": None,
                    "auto_merge": True,
                    "error": None,
                }
            _log.warning(
                "[git] Auto-merge also failed for PR #%d: %s. "
                "Manual merge required: %s",
                pr_number, auto_result["error"], pr_url,
            )
            return {
                "ok": False,
                "pr_url": pr_url,
                "merge_commit": None,
                "auto_merge": False,
                "error": (
                    f"Branch protection blocked merge (HTTP {status_code}); "
                    f"auto-merge also failed: {auto_result['error']}"
                ),
            }
        return {"ok": False, "pr_url": pr_url, "merge_commit": None, "auto_merge": False, "error": merge_resp.text[:400]}

    merge_sha = merge_resp.json().get("sha")

    # Delete remote branch
    requests.delete(f"{api}/git/refs/heads/{branch_name}", headers=headers, timeout=30)

    return {"ok": True, "pr_url": pr_url, "merge_commit": merge_sha, "auto_merge": False, "error": None}


def _github_https_remote(remote_url: str, token: str) -> str | None:
    if "github.com" not in remote_url:
        return None

    https_url = remote_url
    if https_url.startswith("git@github.com:"):
        https_url = https_url.replace("git@github.com:", "https://github.com/")
    elif https_url.startswith("ssh://git@github.com/"):
        https_url = https_url.replace("ssh://git@github.com/", "https://github.com/")

    if https_url.startswith("https://github.com/"):
        return https_url.replace("https://github.com/", f"https://{token}@github.com/")

    return None


def push_branch(
    repo_path: Path, branch_name: str, remote: str = "origin"
) -> dict[str, str | int | bool]:
    result = run_git(
        repo_path,
        ["push", "-u", remote, branch_name],
        check=False,
    )
    if result.returncode == 0:
        return {
            "ok": True,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "transport": "ssh",
        }

    # Retry with force-with-lease on non-fast-forward (task branches are AI-owned)
    if result.returncode != 0 and "rejected" in result.stderr and "non-fast-forward" in result.stderr:
        force_result = run_git(
            repo_path,
            ["push", "--force-with-lease", "-u", remote, branch_name],
            check=False,
        )
        if force_result.returncode == 0:
            return {
                "ok": True,
                "returncode": force_result.returncode,
                "stdout": force_result.stdout.strip(),
                "stderr": force_result.stderr.strip(),
                "transport": "ssh-force",
            }

    token = os.getenv("GITHUB_TOKEN")
    _token_looks_valid = (
        token
        and len(token) >= 20
        and " " not in token
        and not token.lower().startswith("your")
        and not token.lower().startswith("ghp_your")
    )
    if result.returncode != 0 and _token_looks_valid:
        current_url = run_git(
            repo_path, ["remote", "get-url", remote], check=False
        ).stdout.strip()
        https_url = _github_https_remote(current_url, token)
        if https_url:
            run_git(repo_path, ["remote", "set-url", remote, https_url], check=False)
            try:
                https_result = run_git(
                    repo_path,
                    ["push", "-u", remote, branch_name],
                    check=False,
                )
            finally:
                run_git(
                    repo_path, ["remote", "set-url", remote, current_url], check=False
                )

            return {
                "ok": https_result.returncode == 0,
                "returncode": https_result.returncode,
                "stdout": https_result.stdout.strip(),
                "stderr": https_result.stderr.strip(),
                "transport": "https",
            }
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "transport": "ssh",
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
        run_git(repo_path, ["config", "user.name", DEFAULT_GIT_USER_NAME], check=False)
        run_git(repo_path, ["config", "user.email", DEFAULT_GIT_USER_EMAIL], check=False)
        result = run_git(repo_path, ["rev-parse", "--verify", "HEAD"], check=False)
        if result.returncode == 0:
            run_git(repo_path, ["fetch", "origin"], check=False)
            run_git(repo_path, ["pull", "origin", branch], check=False)
    else:
        clone_env = os.environ.copy()
        clone_env["GIT_SSH_COMMAND"] = _build_ssh_command()

        clone_result = subprocess.run(
            ["git", "clone", "--branch", branch, repo_url, str(repo_path)],
            env=clone_env,
            capture_output=True,
            text=True,
        )

        if clone_result.returncode != 0:
            clone_result = subprocess.run(
                ["git", "clone", repo_url, str(repo_path)],
                env=clone_env,
                capture_output=True,
                text=True,
            )

        run_git(repo_path, ["config", "user.name", DEFAULT_GIT_USER_NAME], check=False)
        run_git(repo_path, ["config", "user.email", DEFAULT_GIT_USER_EMAIL], check=False)

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
