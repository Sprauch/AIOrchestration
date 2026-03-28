# Upgrading Agent Orchestrator

## Versioning Policy

Agent Orchestrator follows [Semantic Versioning](https://semver.org/):

- **Patch** (0.1.x): Bug fixes. No config or Redis schema changes. Drop-in upgrade.
- **Minor** (0.x.0): New features, new config fields with defaults. Redis schema compatible. Old configs continue to work — new fields use defaults.
- **Major** (x.0.0): Breaking changes to config format, Redis schema, or CLI interface. Requires migration steps.

## Redis Schema

The orchestrator stamps a schema version in Redis (`orchestrator:schema_version`) on startup. If it detects data from a newer schema version, it refuses to start with a clear error.

**Current schema version: 1**

### Schema version 1 (v0.1.0+)

Redis keys and streams:

| Key pattern | Type | Purpose |
|-------------|------|---------|
| `stream:{channel}` | Stream | Inter-agent message bus |
| `stream:gate-responses:{gate_id}` | Stream | Per-gate approval responses |
| `agent:{id}:status` | String | Agent status (active/busy/stopped) |
| `agent:{id}:heartbeat` | String | Unix timestamp of last activity |
| `agent:{id}:current_task` | String | Current task description |
| `orchestrator:heartbeat` | String (TTL) | Orchestrator liveness |
| `orchestrator:created_prs` | Hash | PR creation audit trail |
| `orchestrator:schema_version` | String | Schema version stamp |

Streams use `MAXLEN ~ 10000` approximate trimming (configurable via `stream_maxlen`).

## Upgrade Procedures

### Patch upgrade (e.g., 0.1.0 -> 0.1.1)

```bash
# Pull latest
git pull

# Reinstall
source agents/.venv/bin/activate
pip install -e ".[dev]"

# Restart
agent-orchestrator run
```

No Redis changes. No config changes. Existing pipeline state is preserved.

### Minor upgrade (e.g., 0.1.x -> 0.2.0)

```bash
# Pull latest
git pull

# Reinstall
source agents/.venv/bin/activate
pip install -e ".[dev]"

# Check for new config fields (optional — defaults are always safe)
diff agents/config.yaml agents/config.yaml.example

# Run preflight to validate
agent-orchestrator preflight

# Restart
agent-orchestrator run
```

New config fields always have defaults, so existing `config.yaml` files work unchanged. New features are opt-in.

### Major upgrade

Major versions may change Redis schema or config format. When this happens:

1. Release notes will document exact migration steps.
2. The orchestrator will refuse to start if it detects an incompatible schema version.
3. A migration script or `agent-orchestrator migrate` command will be provided.

**Before a major upgrade:**
```bash
# Back up Redis data
redis-cli BGSAVE

# Check the UPGRADING.md for the specific version
```

## Rollback

### Patch/Minor rollback

```bash
git checkout v0.1.0    # or the previous version tag
pip install -e ".[dev]"
agent-orchestrator run
```

Redis data from the same schema version is compatible in both directions within a major version.

### Major rollback

If you upgraded to a new major version and the schema changed:

1. Stop the orchestrator.
2. Restore the Redis backup (`redis-cli --pipe < dump.rdb`).
3. Checkout the previous version.
4. Restart.

## Compatibility Matrix

| Software version | Schema version | Config compatible with |
|-----------------|---------------|----------------------|
| 0.1.x | 1 | 0.1.0+ configs |

This table will be extended as new versions are released.
