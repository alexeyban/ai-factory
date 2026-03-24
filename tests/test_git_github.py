"""Tests for shared/git.py GitHub-related helpers."""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.git import (
    _github_repo_slug,
    create_and_merge_github_pr,
    ensure_repo,
    run_git,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo_with_remote(remote_url: str) -> Path:
    """Create a temp git repo with a given origin remote URL."""
    tmpdir = tempfile.mkdtemp()
    repo = Path(tmpdir)
    ensure_repo(repo)
    run_git(repo, ["remote", "add", "origin", remote_url], check=False)
    return repo


def _mock_response(status_code: int, body: dict | str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = status_code < 400
    if isinstance(body, dict):
        resp.json.return_value = body
        resp.text = json.dumps(body)
    else:
        resp.json.return_value = {}
        resp.text = body
    return resp


# ---------------------------------------------------------------------------
# create_and_merge_github_pr — happy path
# ---------------------------------------------------------------------------


def test_create_and_merge_pr_success():
    """PR is created and merged: ok=True, auto_merge=False, merge_commit returned."""
    repo = _make_repo_with_remote("git@github.com:owner/repo.git")

    with patch("shared.git._github_api_token", return_value="ghp_token"), \
         patch("shared.git.requests") as mock_req:

        mock_req.post.return_value = _mock_response(201, {
            "number": 42,
            "html_url": "https://github.com/owner/repo/pull/42",
        })
        mock_req.put.return_value = _mock_response(200, {"sha": "abc123"})
        mock_req.delete.return_value = _mock_response(204, {})

        result = create_and_merge_github_pr(repo, "task-T001", "Add feature")

    assert result["ok"] is True
    assert result["auto_merge"] is False
    assert result["merge_commit"] == "abc123"
    assert result["error"] is None
    assert "pull/42" in result["pr_url"]


# ---------------------------------------------------------------------------
# create_and_merge_github_pr — branch protection (405)
# ---------------------------------------------------------------------------


def test_merge_blocked_405_enables_auto_merge():
    """HTTP 405 from merge triggers auto-merge via GraphQL; returns ok=True, auto_merge=True."""
    repo = _make_repo_with_remote("git@github.com:owner/repo.git")

    with patch("shared.git._github_api_token", return_value="ghp_token"), \
         patch("shared.git.requests") as mock_req:

        # PR creation
        mock_req.post.return_value = _mock_response(201, {
            "number": 7,
            "html_url": "https://github.com/owner/repo/pull/7",
        })
        # Direct merge → blocked
        mock_req.put.return_value = _mock_response(405, "Required status check")
        # GraphQL: fetch node_id
        mock_req.get.return_value = _mock_response(200, {
            "node_id": "PR_NODE_123",
            "number": 7,
            "html_url": "https://github.com/owner/repo/pull/7",
        })
        # GraphQL auto-merge mutation
        mock_req.post.side_effect = [
            _mock_response(201, {"number": 7, "html_url": "https://github.com/owner/repo/pull/7"}),
            _mock_response(200, {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": {"mergeMethod": "MERGE"}}}}}),
        ]

        result = create_and_merge_github_pr(repo, "task-T002", "Branch protection test")

    assert result["ok"] is True
    assert result["auto_merge"] is True
    assert result["merge_commit"] is None
    assert result["error"] is None


def test_merge_blocked_422_logs_warning_and_tries_auto_merge():
    """HTTP 422 (e.g. review required) also triggers auto-merge attempt."""
    repo = _make_repo_with_remote("git@github.com:owner/repo.git")

    with patch("shared.git._github_api_token", return_value="ghp_token"), \
         patch("shared.git.requests") as mock_req:

        mock_req.post.side_effect = [
            _mock_response(201, {"number": 9, "html_url": "https://github.com/owner/repo/pull/9"}),
            _mock_response(200, {"data": {"enablePullRequestAutoMerge": {}}}),
        ]
        mock_req.put.return_value = _mock_response(422, "Review required")
        mock_req.get.return_value = _mock_response(200, {"node_id": "PR_NODE_9", "number": 9, "html_url": "https://github.com/owner/repo/pull/9"})

        result = create_and_merge_github_pr(repo, "task-T003", "Review required test")

    assert result["ok"] is True
    assert result["auto_merge"] is True


def test_merge_blocked_auto_merge_also_fails_returns_ok_false():
    """If both direct merge and auto-merge fail, ok=False with clear error."""
    repo = _make_repo_with_remote("git@github.com:owner/repo.git")

    with patch("shared.git._github_api_token", return_value="ghp_token"), \
         patch("shared.git.requests") as mock_req:

        mock_req.post.side_effect = [
            _mock_response(201, {"number": 5, "html_url": "https://github.com/owner/repo/pull/5"}),
            _mock_response(403, "Forbidden"),  # GraphQL POST
        ]
        mock_req.put.return_value = _mock_response(405, "Status checks required")
        mock_req.get.return_value = _mock_response(200, {"node_id": "PR_5", "number": 5, "html_url": "https://github.com/owner/repo/pull/5"})

        result = create_and_merge_github_pr(repo, "task-T004", "Both fail")

    assert result["ok"] is False
    assert result["auto_merge"] is False
    assert "405" in result["error"] or "branch protection" in result["error"].lower()


# ---------------------------------------------------------------------------
# create_and_merge_github_pr — no token / non-github remote
# ---------------------------------------------------------------------------


def test_no_token_returns_ok_false():
    repo = _make_repo_with_remote("git@github.com:owner/repo.git")
    with patch("shared.git._github_api_token", return_value=None):
        result = create_and_merge_github_pr(repo, "branch", "title")
    assert result["ok"] is False
    assert "GITHUB_TOKEN" in result["error"]


def test_non_github_remote_returns_ok_false():
    repo = _make_repo_with_remote("git@gitlab.com:owner/repo.git")
    with patch("shared.git._github_api_token", return_value="ghp_token"):
        result = create_and_merge_github_pr(repo, "branch", "title")
    assert result["ok"] is False
    assert "github.com" in result["error"]


# ---------------------------------------------------------------------------
# _github_repo_slug
# ---------------------------------------------------------------------------


def test_github_repo_slug_ssh():
    repo = _make_repo_with_remote("git@github.com:myorg/myrepo.git")
    slug = _github_repo_slug(repo)
    assert slug == ("myorg", "myrepo")


def test_github_repo_slug_https():
    repo = _make_repo_with_remote("https://github.com/myorg/myrepo.git")
    slug = _github_repo_slug(repo)
    assert slug == ("myorg", "myrepo")


def test_github_repo_slug_non_github_returns_none():
    repo = _make_repo_with_remote("git@gitlab.com:myorg/myrepo.git")
    assert _github_repo_slug(repo) is None
