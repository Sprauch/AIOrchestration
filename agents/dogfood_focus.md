# Product Focus — Dogfood Mode

This is the agent orchestrator analyzing itself.
Treat it as a real product used by developers and operators, not just an internal codebase.

The goal is to improve how the product feels to use:
- easier to start
- easier to understand
- easier to trust
- easier to recover when something goes wrong

Technical fixes matter when they remove user friction, improve trust, or unblock adoption.
Do not optimize for code quality in isolation.

## Current Priorities

1. **Trust and legibility** — make it obvious what the system is doing, why it is blocked, what changed after challenge/review, and when user action is needed
2. **User experience** — reduce setup friction, confusing states, dead ends, noisy logs, and unclear approval/review flows
3. **Workflow reliability** — fix failures that cause users to lose work, see inconsistent state, or have to manually recover the system
4. **Spend transparency** — make cost, token usage, and inefficient behavior easier for users to understand and control
5. **Adoption and confidence** — improve first-run experience, documentation, defaults, and overall product polish so the system feels safe to try and rely on

## Out of Scope This Cycle

- New agent roles or major new capabilities
- Large visual redesigns that do not improve clarity or trust
- Pure refactors with no clear user or operator value
- Performance work unless it materially improves time-to-value or removes user pain

## Constraints

- Keep changes incremental — no sweeping refactors
- Every change must have tests
- Run project code and tests through the repo virtualenv, using direct binaries like `agents/.venv/bin/pytest tests` instead of relying on a globally installed `python` or `pytest`
- Protected files (agents/config.yaml) require user approval
- Branch prefix must be agent/*
- Changes to prompts, config, and orchestrator core are allowed when they clearly improve product behavior, trust, or dogfooding experience, but keep them narrowly scoped and easy to review
