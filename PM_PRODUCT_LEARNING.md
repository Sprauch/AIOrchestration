# Why a multi-agent coding product is really a workflow product

Most AI coding products are good at isolated execution.

Give them a task and they can produce something useful: a code change, a review, a refactor, a test. But when work becomes multi-step, stateful, and cross-functional, things break down. The model can write code. That was never the problem. Software work is coordination, and that's what most tools miss.

That's the product problem this system is trying to solve.

## The product goal

From a PM standpoint, the goal isn't to create "more agents" or make the workflow feel maximally autonomous. The goal is a system that takes a broad engineering objective, breaks it into specialized roles, moves work through a structured pipeline, and does it with enough safety and observability that a real team could trust it.

This is a product about managed progress, not AI outputs in isolation.

Users don't just want code written. They want meaningful forward motion on real engineering work. That usually requires several different kinds of thinking:

- identifying worthwhile opportunities
- assessing technical feasibility
- implementing safely
- reviewing for regressions
- escalating risky actions
- tracking what happened and what's blocked

A single model can imitate all of those roles, but in practice it collapses them together. When that happens, you get predictable problems:

- weak prioritization
- poor handoffs
- vague accountability
- repeated reasoning
- review loops with little state change
- lower trust in the result

The product answer is role specialization plus orchestration.

## What the PM should actually optimize for

One refinement from working on this system: the PM role should not default to acting like an internal code auditor.

That's an easy failure mode when the product is being dogfooded on its own repository. The codebase is highly visible, so it becomes tempting for the PM to optimize for internal neatness:

- missing tests
- refactors
- implementation consistency
- operator convenience

Those things matter, but they're not the primary product lens.

By default, the PM should optimize for the end user:

- what user problem is being solved
- where the workflow feels confusing or low-trust
- what creates friction in adoption
- what makes outcomes feel useful, legible, and reliable

The codebase is evidence. It is not the product definition.

That distinction matters because without it, the PM drifts toward architect or reviewer behavior. Instead of identifying product value and user-facing friction, the PM starts generating technical cleanup tickets. You end up with weaker role boundaries and a less coherent product workflow.

## Dogfooding is a special context, not the default lens

Dogfooding the orchestrator on itself is useful, but it changes the context of evaluation.

When the system analyzes its own repository, the relevant "user" is no longer a generic end user of an AI coding product. In that mode, the user is closer to:

- the developer using the orchestrator
- the operator trying to trust and control it
- the team adopting it as infrastructure

That's why dogfooding benefits from explicit framing. The system needs to understand that it's looking at an infrastructure product, and that reliability, observability, and developer experience temporarily matter more than end-user feature expansion.

But that should be an explicit mode, not the permanent identity of the PM role.

The cleaner product model:

- default PM lens: end-user value, product clarity, adoption, trust
- dogfood PM lens: infrastructure reliability and developer experience, only when explicitly running in self-analysis mode

Without that distinction, dogfooding distorts the product strategy. Every insight starts to look like an internal systems concern, and the PM slowly stops behaving like a product role.

## The core thesis

The thesis is simple: software work becomes more usable and more governable when AI agents are organized like a lightweight team rather than a single assistant with a very long prompt.

In this system, the PM proposes, the architect scopes and reviews, the developer implements, and the reviewer approves or requests changes. The orchestrator manages routing, safety, approvals, and the overall pipeline.

This matters because the product isn't just trying to increase output. It's trying to create:

- clearer decision boundaries
- better task decomposition
- safer execution
- inspectable progress
- fewer hidden failures

The value isn't autonomy at all costs. It's controlled autonomy.

## Why this product matters

From a product perspective, the strongest value isn't that the system can do one impressive task. It's that it can keep a repeatable workflow moving without requiring a human to manually restate context at every step.

That creates four meaningful outcomes.

### Better throughput

The system can move work from analysis to implementation to review with less re-briefing overhead and less context switching between tools.

### Better quality control

Each stage has a distinct responsibility. That creates natural checkpoints instead of one model generating and approving its own work without friction.

### Better operational safety

The orchestrator can enforce rules around branches, protected files, dangerous commands, approvals, and output validation. This isn't just engineering rigor. Trust is part of usability, so safety is part of the product experience.

### Better observability

Users need to understand what happened, what's stuck, and what needs approval. A system like this only becomes viable if it's inspectable.

## The real challenge

The hard part isn't getting agents to talk to each other.

The hard part is getting them to coordinate without degenerating.

That means avoiding:

- instruction bleed between stages
- oversized messages
- repeated review loops
- vague task assignments
- hidden state carryover
- interfaces that show lots of motion but little meaningful progress

This is where the PM lens matters. Many of these look like technical issues, but they're really product issues because they directly affect trust, clarity, and completion.

If agents exchange huge payloads, the product isn't scaling coordination. It's scaling confusion.

If the monitor shows redundant views or empty metrics, the product isn't visible enough.

If the system loops through the same review cycle without material change, the product isn't governing work effectively.

## The most important learning so far

One of the clearest lessons from this work: agent-to-agent communication cannot be treated like normal chat.

It's an easy trap. It feels natural to let one agent explain its reasoning in prose, pass that to the next agent, and let the workflow evolve conversationally. In practice, that creates a serious coordination problem.

The system starts to amplify language instead of advancing work.

The loop risk came from two dynamics:

- agents exchanged too much free text
- downstream agents reinterpreted that text and generated even more text in response

That creates compounding behavior. A proposal becomes a long review. The review becomes a long implementation brief. The implementation summary becomes a long review request. Each stage inherits not just the task, but the verbosity, ambiguity, and hidden assumptions of the previous stage.

At that point, the system is no longer coordinating work. It's recursively paraphrasing itself.

## What was actually happening

From a workflow perspective, several failure modes started showing up.

### Message inflation

Messages got longer at each stage because agents kept forwarding prior reasoning instead of sending only what the next decision actually needed.

### Instruction bleed

When agents receive large natural-language payloads, they don't always treat them as passive data. Parts of that text can be interpreted as instructions, priorities, or implicit workflow state. That makes behavior less predictable.

### Loop formation

If one stage sends a verbose message and the next stage responds with another verbose interpretation of it, the system can enter a low-value revision loop without meaningful state change.

### Poor observability

Large payloads also make the system harder to inspect. The monitor may show a lot of activity, but activity is not the same as progress.

The product goal is not to maximize generated text. It's to move structured work forward.

## Why the system needed more structure

Orchestration requires a stronger contract than conversation.

Once multiple agents are involved, each handoff has to answer a narrow operational question:

- what is the decision
- what is the next step
- what fields are required for the next stage
- what should not be forwarded

Without that structure, the workflow becomes too interpretive. Every stage has to re-parse the previous stage's language, which increases variance and lowers trust.

That's why the safest direction is to make agent-to-agent messages much more schema-bound and much less natural language.

This is a product design decision, not just an implementation preference. Structured communication reduces ambiguity, improves determinism, and makes the system easier to monitor and govern.

## What works better

The practical answer: make inter-agent messages compact, typed, and stage-specific.

### 1. Make every inter-agent message minimal and typed

Instead of sending long reasoning, send only the fields needed for the next step.

Examples:

- PM -> Architect: `title`, `priority`, `affected_files`, `one_sentence_goal`
- Architect -> Developer: `branch_name`, `files_to_modify`, `acceptance_criteria`, `testing_strategy`
- Reviewer -> Developer: `decision`, `blocking_issues`, `file/line comments`

This keeps each role focused on the handoff it actually owns.

### 2. Cap every text field hard

Verbose fields should be constrained before publish.

Examples:

- `reasoning`: 300-500 chars
- `changes_summary`: 300 chars

## Lightweight PM evaluation rubric

The PM should be judged by product usefulness, not proposal volume.

### What good PM looks like

A strong PM:

- starts from user need, not code neatness
- continuously evaluates the product, not just the codebase
- notices friction, confusion, trust gaps, and repeated manual intervention
- proposes improvements with clear expected outcomes
- maintains a healthy balance between product, UX, trust, reliability, and technical work

A weak PM:

- mostly produces technical cleanup
- describes implementation without user value
- proposes vague improvements like "better visibility" without specifying the user outcome
- waits for the operator to point out obvious product gaps

### Proposal-level evaluation

Each PM proposal should be easy to judge on five dimensions:

1. User problem clarity
- Is the user pain or missing value stated clearly?

2. Expected outcome clarity
- Does the proposal say what should feel better or work better for the user?

3. Evidence quality
- Is the proposal grounded in actual product behavior, repeated friction, or visible gaps?

4. Product relevance
- Is this solving a real product problem, not just an internal code preference?

5. Precision
- Is the proposal specific enough to scope, build, and review?

### Portfolio-level evaluation

Over time, PM quality should also be judged by balance.

The system should not drift into only one kind of work.

Useful proposal target areas:

- `product`
- `ux`
- `trust`
- `workflow`
- `reliability`
- `technical`
- `cost`
- `onboarding`
- `feature`

If PM mostly produces `technical` proposals, the role is underperforming as a product function.

### Signals that PM is improving

The PM is getting stronger when:

- operator-discovered product gaps become less frequent
- proposals have clearer user problems and expected outcomes
- fewer proposals get sent back for ambiguity
- more proposals are obviously tied to product trust, clarity, adoption, or workflow quality
- proposal mix stays healthy instead of collapsing into technical maintenance

### Practical standard

The PM should be the agent most responsible for user value.

That means:

- customer first
- iterative evaluation
- outcome-oriented proposals
- explicit labeling of proposal intent

The role is not to generate more work.
The role is to generate the right work.
- `notes`: 200 chars
- arrays: max 5-10 items

Anything longer should be truncated or replaced with a short summary.

In this kind of product, message size is not neutral. Oversized payloads are a reliability risk.

### 3. Stop forwarding full prior outputs

Agents should not pass raw previous agent text onward unless absolutely necessary.

Instead of forwarding:

- a full review narrative
- a full proposal write-up
- a long implementation summary

the system should pass compact workflow state:

- `decision=changes_requested`
- `blocking_issues=[...]`
- `next_action=fix`

That preserves what matters without dragging the full prior conversation into the next stage.

### 4. Separate machine payload from human trace

This is one of the most useful distinctions in the whole product.

The machine workflow needs compact structured payloads.

Humans may still want detailed reasoning, but that belongs in `cli-traces`, logs, or a dedicated inspection surface, not in the operational message path.

That separation improves both sides:

- agents get cleaner inputs
- humans still get explainability

### 5. Add loop guards at the workflow level

The product should track per `thread_id` state:

- current stage
- revision count
- last decision
- last payload hash

Then it should block or escalate when it sees:

- the same decision repeated with no material change
- more than N review/change cycles
- re-emitting essentially identical payloads

This prevents activity theater from being mistaken for progress.

### 6. Make reviewer and architect outputs binary when possible

These roles should prefer outcomes like:

- `approved`
- `needs_revision`
- `changes_requested`

with short bullets, not essays.

The more binary and bounded those decisions are, the easier the workflow is to route, monitor, and recover when something goes wrong.

### 7. Reject oversized outgoing payloads

The product should have output safety for message size, not only for dangerous content.

Examples:

- max serialized payload size: 4 KB
- max string field size per key

If exceeded, the system should truncate, summarize, or block.

That turns verbosity into an explicit control surface rather than an accidental behavior.

## What this changes in the product definition

This learning sharpens the product definition considerably.

The product is not only about making agents capable. It's about making agent collaboration operationally legible.

That means favoring:

- structured handoffs over rich prose
- bounded payloads over open-ended narratives
- explicit workflow state over inferred conversational state
- traces for explanation, schemas for execution

If everything is treated as conversation, the system becomes harder to control.

If execution is structured and explanation is separated, the system becomes much more reliable.

I think that's one of the most important product distinctions in the whole project.

## What this suggests for the roadmap

At the implementation level, this points toward a few clear improvements:

- add payload normalization before publish in `agents/core/base_agent.py`
- shrink proposal payloads in `agents/roles/pm_agent.py`
- keep architect outputs limited to implementation-critical fields in `agents/roles/architect_agent.py`
- make reviewer responses short and blocking-focused in `agents/roles/reviewer_agent.py`
- optionally enforce payload size constraints in `agents/core/message.py`

But the deeper point isn't any one file change.

Multi-agent systems need stronger communication contracts than single-agent tools. Once you understand that, the product becomes easier to reason about, easier to operate, and easier to trust.

## The narrative I'd use externally

This is a system for turning AI coding from isolated model output into managed software execution.

It treats software work as a pipeline, not a prompt.

It introduces specialization, validation, review, safety controls, and monitoring so that AI can contribute to real engineering workflows with less manual coordination overhead and more operational trust.

The ambition isn't to replace engineering judgment. It's to make autonomous engineering work more structured, measurable, and governable.

## The PM takeaway

The deepest learning here is that this isn't really an agent product. It's a workflow product.

Its success depends less on how impressive any one agent looks in isolation, and more on whether the overall system:

- moves work forward
- avoids self-generated complexity
- surfaces state clearly
- fails safely
- earns user trust over repeated use

That's the real bar.

If the system can do that consistently, it has a credible reason to exist.
