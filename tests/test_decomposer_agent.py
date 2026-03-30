from agents.decomposer.agent import estimate_tokens, normalize_task_contract
from shared.git import _github_https_remote


def test_normalize_task_contract_adds_standard_schema() -> None:
    task = {
        "title": "Add fallback push",
        "description": "Implement HTTPS fallback when SSH push fails.",
        "dependencies": "shared/git.py",
    }

    normalized = normalize_task_contract(task, project_context={"project_name": "demo"})

    assert normalized["task_id"]
    assert normalized["type"] == "feature"
    assert normalized["input"]["files"] == []
    assert "project_name" in normalized["input"]["context"]
    assert normalized["output"]["expected_result"] == task["description"]
    assert normalized["verification"]["method"] == "review"
    assert normalized["dependencies"] == ["shared/git.py"]


def test_estimate_tokens_uses_rough_char_heuristic() -> None:
    assert estimate_tokens("abcd") == 1


def test_github_https_remote_converts_ssh_url() -> None:
    # Token must NOT appear in the returned URL (passed via Authorization header instead)
    result = _github_https_remote("git@github.com:owner/repo.git")
    assert result == "https://github.com/owner/repo.git"
    assert "TOKEN" not in (result or "")
