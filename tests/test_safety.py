"""Tests for SafetyChecker."""

import pytest

from agents.core.safety import SafetyChecker, SafetyConfig, SafetyViolation


@pytest.fixture
def checker():
    return SafetyChecker(SafetyConfig(
        blocked_patterns=["rm -rf", "DROP TABLE", "git push --force"],
        protected_files=[".env", "vercel.json"],
        branch_prefix="agent/",
        never_push_to=["main", "master"],
        human_approval_required=["delete_files", "large_change"],
        max_files_per_change=5,
    ))


def test_blocked_pattern_raises(checker):
    with pytest.raises(SafetyViolation, match="blocked_pattern"):
        checker.check_prompt("Let me rm -rf the directory")


def test_blocked_pattern_case_insensitive(checker):
    with pytest.raises(SafetyViolation):
        checker.check_prompt("DROP TABLE users")


def test_safe_prompt_passes(checker):
    checker.check_prompt("Add a new test for the API endpoint")


def test_protected_file_raises(checker):
    with pytest.raises(SafetyViolation, match="protected_file"):
        checker.check_files([".env"])


def test_protected_file_nested_path(checker):
    with pytest.raises(SafetyViolation, match="protected_file"):
        checker.check_files(["config/.env"])


def test_safe_files_pass(checker):
    result = checker.check_files(["app/main.py", "tests/test_api.py"])
    assert result is None


def test_large_change_escalates(checker):
    files = [f"file{i}.py" for i in range(10)]
    result = checker.check_files(files)
    assert result == "large_change"


def test_branch_prefix_valid(checker):
    checker.check_branch("agent/fix-tests")


def test_branch_prefix_invalid(checker):
    with pytest.raises(SafetyViolation, match="branch_prefix"):
        checker.check_branch("feature/my-branch")


def test_push_target_blocked(checker):
    with pytest.raises(SafetyViolation, match="never_push_to"):
        checker.check_push_target("main")


def test_push_target_allowed(checker):
    checker.check_push_target("agent/my-branch")


def test_needs_approval(checker):
    assert checker.needs_approval("delete_files") is True
    assert checker.needs_approval("large_change") is True
    assert checker.needs_approval("some_other_action") is False
