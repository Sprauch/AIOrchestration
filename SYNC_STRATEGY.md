# Synchronization Strategy

## Goal

Move the orchestrator from an inferred state model toward an explicit state model.

Today the system blends:
- event history
- scheduling state
- derived UI state

That works, but it creates drift:
- blocked or abandoned threads can keep occupying active work
- replay is used too often to recover current truth
- queued and in-progress work are easy to conflate
- the UI sometimes shows state correctly but cannot explain transitions clearly

The target model is:
- streams are the event log
- thread state is the authoritative source of truth
- indexes are the scheduling/query layer

## Core Principles

1. Streams are append-only history
- Keep workflow streams for audit, replay, and timeline rendering.
- Do not rely on stream replay as the normal source of current truth.

2. Each thread has one authoritative state record
- A thread record should answer:
  - what stage it is in
  - whether it is queued, claimed, blocked, or terminal
  - who owns it
  - what revision is current
  - whether it has a branch or PR

3. Scheduling state is explicit
- The scheduler should not infer active work from event history alone.
- Queued and in-progress work must be represented separately.

4. Transitions are atomic
- Each workflow move should update:
  - the thread record
  - stage indexes
  - claim state
  - the event log

5. Terminal outcomes clear scheduling state immediately
- Blocked, abandoned, merged, skipped, and other terminal states must stop consuming WIP.

## Proposed Data Model

### 1. Thread Record

Per-thread authoritative hash, for example:

`orchestrator:thread:{thread_id}`

Suggested fields:
- `thread_id`
- `current_stage`
- `status`
- `queued_stage`
- `claimed_stage`
- `claimed_by`
- `queued_at`
- `claimed_at`
- `updated_at`
- `revision_id`
- `review_cycles`
- `branch_name`
- `pr_status`
- `blocked_reason`
- `terminal_reason`
- `label`

### 2. Scheduling Indexes

Sets for fast scheduling and UI queries, for example:

- `orchestrator:threads:queued:proposals`
- `orchestrator:threads:claimed:proposals`
- `orchestrator:threads:queued:tasks`
- `orchestrator:threads:claimed:tasks`
- `orchestrator:threads:queued:reviews`
- `orchestrator:threads:claimed:reviews`
- `orchestrator:threads:blocked`
- `orchestrator:threads:terminal`

These replace the idea that one flat active-work set is enough.

### 3. Event Log

Keep the existing workflow streams:
- `proposals`
- `reviews`
- `tasks`
- `review-requests`
- `review-results`
- `progress`
- `human-gates`
- `system`
- `cli-traces`

These remain important, but they become history rather than the main runtime truth.

## Transition Model

Define explicit transitions such as:

- `proposal_queued`
- `proposal_claimed`
- `proposal_reviewed`
- `task_queued`
- `task_claimed`
- `review_queued`
- `review_claimed`
- `rework_queued`
- `blocked`
- `abandoned`
- `pr_created`
- `pr_merged`
- `pr_skipped`
- `completed`

Each transition helper should:
- validate the current thread state
- update the thread record
- move the thread between indexes
- write the event to the appropriate stream

## Why This Is Better

This model reduces several current failure modes:

- stale active work after terminal outcomes
- proposal/task backlog inflated by blocked threads
- restart rebuild ambiguity
- backpressure based on partially dead work
- UI confusion between current state and recent transition

It also makes it easier to answer product questions:
- what is queued?
- what is currently being worked?
- what is blocked?
- what changed recently?

## Migration Plan

### Phase 1: Introduce Authoritative Thread State
- Add the per-thread state record alongside existing streams.
- Keep current active-work and claim state running in parallel.

### Phase 2: Centralize Transition Helpers
- Route workflow transitions through shared helper functions.
- Ensure helpers update thread record, indexes, and history together.

### Phase 3: Move Gating and Recovery
- Base backpressure and stale-work recovery on explicit thread state and indexes.
- Reduce dependence on stream replay for normal scheduling decisions.

### Phase 4: Move UI Derivation
- Prefer thread-state as the source for:
  - current stage
  - status
  - latest change
  - attention and exception views

### Phase 5: Retire Ad Hoc Synchronization Paths
- Remove or shrink older keys and logic once the new model is authoritative.

## Cleanup Targets

Once the thread-state model is authoritative, revisit:
- flat active-work sets as the main scheduling truth
- scattered claim keys without stage semantics
- replay-heavy rebuild paths
- special-case terminal cleanup logic spread across multiple modules

## Atomicity Mechanism

Transitions must update multiple Redis keys together. Two viable approaches:

### Option A: Lua Scripts (Recommended for Phase 1-2)

Each transition is a single Lua script that runs atomically on the Redis server:

```lua
-- Example: proposal_claimed
local thread_key = KEYS[1]     -- orchestrator:thread:{tid}
local queued_key = KEYS[2]     -- orchestrator:threads:queued:proposals
local claimed_key = KEYS[3]    -- orchestrator:threads:claimed:proposals

local current = redis.call('HGET', thread_key, 'status')
if current ~= 'queued' then return 0 end

redis.call('HSET', thread_key, 'status', 'claimed', 'claimed_by', ARGV[1], 'claimed_at', ARGV[2])
redis.call('SREM', queued_key, ARGV[3])
redis.call('SADD', claimed_key, ARGV[3])
return 1
```

Pros: Truly atomic, no race conditions, works with single Redis instance.
Cons: Lua scripts are hard to test and debug, no cross-key transactions in Redis Cluster.

### Option B: Pipeline + Validation

Use Redis MULTI/EXEC for batching, with a pre-check read:

```python
async def transition(redis, thread_id, from_status, to_status, updates, index_moves):
    current = await redis.hget(f"orchestrator:thread:{thread_id}", "status")
    if current != from_status:
        raise StateConflict(f"Expected {from_status}, got {current}")
    pipe = redis.pipeline()
    pipe.hset(f"orchestrator:thread:{thread_id}", mapping=updates)
    for remove_from, add_to in index_moves:
        pipe.srem(remove_from, thread_id)
        pipe.sadd(add_to, thread_id)
    await pipe.execute()
```

Pros: Easier to read and test, works well for single-operator model.
Cons: Not atomic — race between read and write. Acceptable when there's one orchestrator.

**Recommendation:** Start with Lua for the thread cycle guard pattern (already proven) and pipeline for everything else. Move to full Lua if concurrency issues appear.

## Consumer Group Evolution

The current model uses Redis Streams consumer groups for message delivery. With thread records as truth, the question is whether agents should still consume from streams or poll scheduling indexes.

### Current: Stream-Driven

```
PM publishes PROPOSAL → stream:proposals → architect XREADGROUP → processes
```

Agents are passive — they wait for messages to arrive in their subscribed streams.

### Target: Index-Driven (Phase 3+)

```
Transition: proposal_queued → thread record updated, added to queued:proposals index
Architect polls: SRANDMEMBER orchestrator:threads:queued:proposals → claims → processes
```

Agents are active — they pull work from the scheduling index.

### Why This Matters

Stream-driven delivery is fine when there's one consumer per role. But it creates problems with:
- Redelivery after crashes (XAUTOCLAIM is a blunt instrument)
- Backpressure measured by stream depth instead of actual queue depth
- Consumer group state that outlives the agent process

Index-driven delivery solves all three: agents claim from a set (atomic SPOP or SRANDMEMBER + HSETNX), process, and transition. No consumer group state to manage.

### Migration Path

Keep streams for Phase 1-2 — they work and agents already use them. In Phase 3, add an alternative `poll_work()` method alongside `subscribe_group()`. Agents can opt into either. Retire stream consumption in Phase 5 when the index model is proven.

## Thread Record Lifecycle

### State Machine

A thread moves through these states (not stages — states describe its scheduling disposition):

```
created → queued → claimed → processing → [outcome]
                                            ├→ queued (rework)
                                            ├→ blocked
                                            ├→ abandoned
                                            └→ completed
```

`stage` tracks where in the pipeline (proposals, tasks, reviews).
`status` tracks the scheduling state (queued, claimed, blocked, terminal).

This is cleaner than the current model where `stage` and `status` are tangled — "rework" is used as both a stage and a status, and "blocked" means different things in different contexts.

### Retention and Garbage Collection

Thread records accumulate. Policy:

- **Active threads** (queued, claimed, processing): no expiry
- **Terminal threads** (completed, abandoned, blocked): keep for 7 days, then archive
- **Archive**: move to a separate hash or delete. The event streams already serve as long-term history.

Implementation: A periodic cleanup task (in the orchestrator heartbeat loop or a separate cron) scans `orchestrator:threads:terminal` and deletes thread records older than the retention period.

No TTL on individual thread records — use explicit cleanup so the operator can always inspect recent history.

## Concurrency Model

### Single Orchestrator (Current)

One orchestrator process manages all agents. Transitions are effectively serialized through the orchestrator's event loop. Pipeline + validation is sufficient for atomicity.

### Multiple Orchestrators (Future)

If multiple orchestrator instances share the same Redis:
- Thread records must use optimistic concurrency (check-and-set via Lua)
- Claims must use HSETNX (already implemented for thread claims)
- Index updates must be idempotent (SADD/SREM are naturally idempotent)

The current Lua-based thread guard pattern is already compatible with this. The main work would be ensuring all transition helpers use the same pattern.

**Position:** Design for single orchestrator, implement in a way that doesn't preclude multiple. Use Lua for contested operations (claims, cycle increments), pipeline for uncontested ones (status updates after successful processing).

## Recovery Model

### Current: Rebuild from History

On startup, `_rebuild_active_work_sets()` replays all stream events to reconstruct state. This works but is:
- Slow for large stream history
- Lossy if MAXLEN trimming has deleted old events
- Fragile if events are ambiguous (duplicate proposals, requeued messages)

### Target: Read from Thread Records

On startup, the orchestrator reads thread records directly:

```python
async def _recover_state(self):
    # Thread records ARE the state — no replay needed
    for stage in ("proposals", "tasks", "reviews"):
        queued = await redis.smembers(f"orchestrator:threads:queued:{stage}")
        claimed = await redis.smembers(f"orchestrator:threads:claimed:{stage}")
        # Re-queue any claimed threads (previous owner is dead)
        for tid in claimed:
            await self._transition(tid, "claimed", "queued")
```

This is O(active threads) instead of O(total stream history). It's also deterministic — no ambiguity from duplicate events or trimmed history.

### Validation

After recovery, optionally compare thread record state against recent stream events to detect drift. Log warnings if they disagree, but trust the thread record.

## What This Changes in Practice

For each current pain point from this session:

| Problem | Current Fix | With Thread Records |
|---------|-------------|-------------------|
| Duplicate proposals | Dedup cache + stall repair guard | Transition rejects duplicate: "already queued" |
| Stale WIP counts | Rebuild from streams | SCARD on queued index — always accurate |
| Ghost branches | Startup scan of thread_branches | Thread record tracks branch; terminal transition cleans it |
| Reviewer can't see branch | Prompt-level verification | Thread record has `branch_name`; reviewer reads it directly |
| Blocked thread occupies WIP | Terminal cleanup scattered across modules | `blocked` transition atomically removes from all indexes |
| PR status confusion | Separate PR audit hash | Thread record has `pr_status` field |

## Summary

The main direction is:

- streams for history
- thread record for truth
- indexes for scheduling
- atomic transitions for consistency

Extensions:
- Lua scripts for contested transitions, pipeline for uncontested
- Consumer groups → index-driven polling (Phase 3+)
- Explicit state machine with clean state/stage separation
- 7-day retention on terminal threads, periodic garbage collection
- Recovery reads thread records directly instead of replaying streams
- Designed for single orchestrator, compatible with multiple

That gives the system a declared state model instead of an inferred one.
