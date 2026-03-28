"""Safety checker — enforces blocked patterns, protected files, and branch rules."""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SafetyConfig(BaseModel):
    blocked_patterns: list[str] = Field(default_factory=list)
    protected_files: list[str] = Field(default_factory=list)
    branch_prefix: str = "agent/"
    never_push_to: list[str] = Field(default_factory=lambda: ["main", "master"])
    human_approval_required: list[str] = Field(default_factory=list)
    max_files_per_change: int = 10


class SafetyViolation(Exception):
    """Raised when a hard-block safety rule is violated."""

    def __init__(self, rule: str, detail: str):
        self.rule = rule
        self.detail = detail
        super().__init__(f"Safety violation [{rule}]: {detail}")


class SafetyChecker:
    """Evaluates messages and actions against safety rules.

    Hard blocks raise SafetyViolation immediately.
    Escalatable actions return an action string for human approval gating.
    """

    def __init__(self, config: SafetyConfig):
        self.config = config
        self._blocked_re = [
            re.compile(re.escape(p), re.IGNORECASE) for p in config.blocked_patterns
        ]

    def check_prompt(self, prompt: str) -> None:
        """Check a CLI prompt for blocked patterns. Raises on violation."""
        for pattern, raw in zip(self._blocked_re, self.config.blocked_patterns):
            if pattern.search(prompt):
                raise SafetyViolation("blocked_pattern", f"Prompt contains '{raw}'")

    def check_files(self, files: list[str]) -> str | None:
        """Check file list for protected files and large changes.

        Returns None if OK, or an escalation action string if human approval needed.
        Raises SafetyViolation for hard-blocked files.
        """
        for f in files:
            for protected in self.config.protected_files:
                if f == protected or f.endswith(f"/{protected}"):
                    raise SafetyViolation("protected_file", f"Cannot modify {f}")

        if len(files) > self.config.max_files_per_change:
            if "large_change" in self.config.human_approval_required:
                return "large_change"

        return None

    def check_branch(self, branch: str) -> None:
        """Ensure branch follows the required prefix. Raises on violation."""
        if not branch.startswith(self.config.branch_prefix):
            raise SafetyViolation(
                "branch_prefix",
                f"Branch '{branch}' must start with '{self.config.branch_prefix}'",
            )

    def check_push_target(self, target: str) -> None:
        """Ensure we never push to protected branches. Raises on violation."""
        if target in self.config.never_push_to:
            raise SafetyViolation("never_push_to", f"Cannot push to '{target}'")

    def needs_approval(self, action: str) -> bool:
        """Check if an action requires human approval."""
        return action in self.config.human_approval_required

    def classify_files(self, files: list[str]) -> list[dict[str, str | bool]]:
        """Classify files as protected/sensitive for gate context.

        Returns a list of dicts with 'path' and 'protected' keys.
        """
        result = []
        for f in files:
            is_protected = any(
                f == p or f.endswith(f"/{p}") for p in self.config.protected_files
            )
            result.append({"path": f, "protected": is_protected})
        return result


def build_gate_context(
    *,
    operation: str,
    branch: str | None = None,
    files: list[dict[str, str | bool]] | None = None,
    file_count: int | None = None,
    safety_summary: str = "passed",
    escalation_reason: str = "",
    extra: dict | None = None,
) -> dict:
    """Build a structured context dict for HUMAN_GATE payloads.

    Parameters
    ----------
    operation : str
        Type of operation (e.g. "large_change", "protected_file", "create_pr").
    branch : str | None
        Affected git branch, if any.
    files : list | None
        Classified file list from SafetyChecker.classify_files().
    file_count : int | None
        Total number of affected files.
    safety_summary : str
        Overall safety check result ("passed", "escalated", etc.).
    escalation_reason : str
        Why this gate was triggered.
    extra : dict | None
        Additional context fields specific to the operation.
    """
    ctx: dict = {
        "operation": operation,
        "safety_summary": safety_summary,
        "escalation_reason": escalation_reason,
    }
    if branch is not None:
        ctx["branch"] = branch
    if files is not None:
        ctx["files"] = files[:20]  # cap to avoid huge payloads
    if file_count is not None:
        ctx["file_count"] = file_count
    if extra:
        ctx.update(extra)
    return ctx
