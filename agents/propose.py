"""Submit a proposal to the architect from a file.

THE OTHER HALF OF MANUAL MODE. The dashboard form is for when you are away from the
machine; this is for a proposal worth writing properly, in an editor, in a file you can
keep in version control and revise.

The file is Markdown, because a proposal is prose and prose should not be typed into
JSON. The first `# heading` is the title; every `## heading` after it fills the field of
the same name. Unknown sections are ignored rather than rejected, so notes to yourself do
no harm.

    # Stop showing $0.00 for cards with no price

    ## user problem
    A wishlist row reads $0.00 when the price is merely unknown...

    ## proposed change
    Replace the sentinel with an explicit per-entry price state...

    ## target area
    product

JSON is accepted too, for anything generating these rather than writing them.

Published straight to the proposals stream through the message bus: no dashboard needs to
be running, and the envelope is identical to the one the form produces.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Mirrors PMAgent.USER_FACING_TARGETS. A user-facing proposal reaches the product
# designer first; everything else goes straight to the architect.
USER_FACING_TARGETS = {
    "product", "ux", "trust", "onboarding", "workflow", "adoption", "feature",
}

# Section heading (normalized: lowercase, spaces or underscores) -> payload field.
_FIELDS = {
    "user problem": "user_problem",
    "problem": "user_problem",
    "description": "description",
    "proposed change": "proposed_change",
    "change": "proposed_change",
    "rationale": "rationale",
    "why": "rationale",
    "expected user outcome": "expected_user_outcome",
    "outcome": "expected_user_outcome",
    "success signal": "success_signal",
    "target area": "target_area",
    "category": "category",
    "priority": "priority",
    "estimated effort": "estimated_effort",
    "effort": "estimated_effort",
    "affected files": "affected_files",
    "files": "affected_files",
}

_LIST_FIELDS = {"affected_files"}
_INT_FIELDS = {"priority"}


def parse_markdown(text: str) -> dict:
    """Turn a proposal Markdown file into a payload dict."""
    out: dict = {}
    title_match = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    if title_match:
        out["title"] = title_match.group(1).strip()

    # Split on ## headings, keeping each heading with the body that follows it.
    parts = re.split(r"^##\s+(.+?)\s*$", text, flags=re.MULTILINE)
    for i in range(1, len(parts) - 1, 2):
        key = parts[i].strip().lower().replace("_", " ")
        field = _FIELDS.get(key)
        if not field:
            continue
        body = parts[i + 1].strip()
        if field in _LIST_FIELDS:
            out[field] = [
                re.sub(r"^[-*]\s*", "", line).strip()
                for line in body.splitlines()
                if line.strip()
            ]
        elif field in _INT_FIELDS:
            digits = re.search(r"\d+", body)
            out[field] = int(digits.group()) if digits else 3
        else:
            out[field] = body
    return out



# ── References into the project's own registers ────────────────────────────
#
# The proposals worth running are usually ALREADY WRITTEN DOWN. This project keeps
# defects in docs/register/ and planned work in docs/backlog/, each entry a Markdown
# file with YAML front matter, and re-typing one into a proposal form would be both
# wasteful and a chance to get it wrong.
#
# So `propose R144` means: read docs/register/**/R144.md, and send THAT.

REFERENCE_RE = re.compile(r"^([RB])(\d+)$", re.IGNORECASE)

# R is a defect register entry, B is planned work. They get different default framing
# because a defect's problem statement is the fault itself, where a backlog item's is
# the opportunity.
_REF_DEFAULTS = {
    "R": {"target_area": "reliability", "category": "reliability", "priority": 1},
    "B": {"target_area": "product", "category": "product", "priority": 3},
}


def _split_front_matter(text: str) -> tuple[dict, str]:
    """Return (front matter dict, body). Tolerates a file with no front matter."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw, body = text[3:end], text[end + 4:]
    meta: dict = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip()
    return meta, body.lstrip("\n")


def find_reference(ref: str, working_dir: str | Path) -> Path | None:
    """Locate the file for a reference like R144 or B67, or None."""
    m = REFERENCE_RE.match(ref.strip())
    if not m:
        return None
    ident = f"{m.group(1).upper()}{m.group(2)}"
    root = Path(working_dir)
    folder = "register" if ident.startswith("R") else "backlog"
    # Entries live one level down, by domain: docs/register/core/R144.md
    matches = sorted(root.glob(f"docs/{folder}/**/{ident}.md"))
    return matches[0] if matches else None


def load_reference(ref: str, working_dir: str | Path) -> dict:
    """Build a proposal from an existing register or backlog entry."""
    path = find_reference(ref, working_dir)
    ident = ref.strip().upper()
    if path is None:
        folder = "register" if ident.startswith("R") else "backlog"
        raise ValueError(
            f"{ident} not found under docs/{folder}/ in {working_dir}. "
            "Check the id, or pass a proposal file instead."
        )

    meta, body = _split_front_matter(path.read_text(encoding="utf-8"))
    defaults = _REF_DEFAULTS["R" if ident.startswith("R") else "B"]

    title = meta.get("title") or ident
    data = {
        # The id leads the title so every downstream view names the source of truth.
        "title": f"{ident}: {title}",
        # The ENTIRE entry, untruncated. It is the best context that exists for this
        # work — somebody already did the thinking, and cutting it here would throw
        # that away at the one moment it is needed.
        "user_problem": body.strip(),
        "description": body.strip(),
        "rationale": (
            f"Source of truth: {path.as_posix()} ({ident}). "
            f"status: {meta.get('status', 'unknown')}; class: {meta.get('class', 'unknown')}; "
            f"domain: {meta.get('domain', 'unknown')}. "
            "Update that entry rather than restating it."
        ),
        "target_area": meta.get("target_area") or defaults["target_area"],
        "category": meta.get("category") or defaults["category"],
        "priority": defaults["priority"],
        "estimated_effort": "medium",
        # `area` is free text naming files, often with line numbers. Keep the paths.
        "affected_files": sorted({
            token.split(":")[0]
            for token in re.findall(r"[\w./-]+\.\w+(?::\d+(?:-\d+)?)?", meta.get("area", ""))
            if "/" in token
        }),
    }
    return data


def load_proposal_file(path: str | Path) -> dict:
    """Read a proposal from .md or .json. Raises ValueError with a usable message."""
    p = Path(path)
    if not p.exists():
        raise ValueError(f"no such file: {p}")
    text = p.read_text(encoding="utf-8")

    if p.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{p} is not valid JSON: {exc}") from exc
    else:
        data = parse_markdown(text)

    if not data.get("title"):
        raise ValueError(
            f"{p} has no title. Markdown needs a line starting with '# ', "
            "JSON needs a \"title\" key."
        )
    if not data.get("user_problem"):
        raise ValueError(
            f"{p} has no user problem. Add a '## user problem' section — a proposal "
            "that cannot say whose problem it solves is the kind the architect rejects "
            "on its premise."
        )
    return data


def build_payload(data: dict) -> tuple[dict, str]:
    """Normalize a parsed proposal. Returns (payload, recipient_role)."""
    target_area = str(data.get("target_area", "product")).strip().lower()
    payload = {
        "title": str(data["title"]),
        "target_area": target_area,
        "user_problem": str(data["user_problem"]),
        "description": str(data.get("description") or data["user_problem"]),
        "proposed_change": str(data.get("proposed_change", "")),
        "rationale": str(data.get("rationale", "")),
        "expected_user_outcome": str(data.get("expected_user_outcome", "")),
        "success_signal": str(data.get("success_signal", "")),
        "priority": int(data.get("priority", 2) or 2),
        "affected_files": list(data.get("affected_files", []) or []),
        "estimated_effort": str(data.get("estimated_effort", "medium")),
        "category": str(data.get("category", target_area)),
    }
    recipient = "product_designer" if target_area in USER_FACING_TARGETS else "architect"
    return payload, recipient
