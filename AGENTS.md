# Subagent Team

Specialized Claude Code agents for the Hydrant Command Center. Each agent has a focused role, scoped tool access, and a defined output format.

## Agents

### Core Agents

| Agent             | Model  | Purpose                                                                   |
| ----------------- | ------ | ------------------------------------------------------------------------- |
| **code-reviewer** | Sonnet | Reviews diffs for bugs, security, style, and Hydrant-specific conventions |
| **test-writer**   | Sonnet | Writes pytest tests for analytics, ETL, and database modules              |
| **pr-manager**    | Sonnet | Reviews PRs, validates conventional commits, manages merge workflow       |
| **debugger**      | Sonnet | Traces errors through ETL → DB → analytics → dashboard layers             |

### Project-Specific Agents

| Agent            | Model  | Purpose                                                              |
| ---------------- | ------ | -------------------------------------------------------------------- |
| **data-analyst** | Sonnet | Queries SQLite to analyze sales, forecasts, inventory, and retention |
| **etl-monitor**  | Haiku  | Checks sync health, data freshness, and pipeline status              |
| **ui-verifier**  | Haiku  | Screenshots all 6 dashboard pages to verify rendering after deploys  |

## How to Invoke

Agents are invoked automatically by Claude Code when your request matches their description. You can also reference them explicitly:

```
# Review my changes
"Review the code I just changed"  → code-reviewer

# Write tests
"Write tests for the waterfall module"  → test-writer

# Check PRs
"Review open PRs"  → pr-manager

# Debug an issue
"The reorder page shows wrong dates"  → debugger

# Analyze data
"What were top selling SKUs last month?"  → data-analyst

# Check pipeline health
"Is the ETL running? When did data last sync?"  → etl-monitor

# Verify the dashboard
"Screenshot all pages and check they render"  → ui-verifier
```

## Recommended Workflows

### After implementing a feature

1. **code-reviewer** — review your changes
2. **test-writer** — write tests for new code
3. **ui-verifier** — verify the dashboard renders correctly

### After a deploy

1. **ui-verifier** — screenshot all pages
2. **etl-monitor** — confirm data pipelines are healthy

### Investigating a bug

1. **debugger** — trace root cause
2. **data-analyst** — verify data in the database
3. **etl-monitor** — check if the issue is upstream

### PR workflow

1. **code-reviewer** — review the diff
2. **pr-manager** — check CI status and merge readiness

## Agent Files

All agent definitions live in `.claude/agents/`:

```
.claude/agents/
├── code-reviewer.md
├── test-writer.md
├── pr-manager.md
├── debugger.md
├── data-analyst.md
├── etl-monitor.md
└── ui-verifier.md
```

Edit any `.md` file to customize agent behavior. Changes take effect on next Claude Code invocation.
