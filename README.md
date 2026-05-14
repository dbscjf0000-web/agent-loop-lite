# agent-loop-lite

`agent-loop-lite` is a small reference implementation of the RPIVJ loop:

```text
R = Read       task, prior feedback, success criteria
P = Plan       concrete next implementation plan + verify.sh
I = Implement  workers edit workspace/ freely (tools, not stdout)
V = Validate   run verify.sh (or plan.md commands) for objective gate
J = Judge      promote best (git tag), roll back regressions (git reset), decide stop/redo
```

## v2: Git-Backed Workspace + Capability Tiers

The original (v1) design forced every worker to emit file contents through
stdout as `# file: <path>` fenced blocks. That contract caused real
failures: large files truncated to placeholders, partial edits unsupported,
no rollback on the first cycle, and Planner/Critic blindfolded with no
read access to the workspace.

v2 fixes the root cause: **don't parse the agent's stdout — observe the
filesystem.**

```
                      v1                          v2
                    ──────                      ──────
Workspace state     plain folder                git repo (auto-init)
Change detection    parse `# file:` blocks      git diff + git status
Rollback target     copy `best/` folder         git reset --hard <tag>
Promote PASS        copy workspace → best/      git tag best-cycle-N
Builder tools       forbidden                   full read/write/exec
Planner tools       forbidden                   read-only (ls/grep/cat/git)
Critic tools        forbidden                   read-only diagnostic
verify              inlined heredoc in plan     separate verify.sh file
Worker context      only prior hint string      state.json + git log + history
```

### Capability tiers

```
                  Read    Write   Bash     Output artifact
                  ────    ─────   ────     ───────────────
Planner           ✓       (only plan.md/verify.sh)         plan.md, verify.sh
Builder           ✓       ✓       ✓                        any workspace file
Critic            ✓       ✗ (auto-revert)  diagnostic     judge.json
```

If the Critic accidentally writes to workspace/, the loop detects the
uncommitted change with `git status --porcelain` and `git reset --hard`s
back to the Critic-pre snapshot.

### Wipe protection

The legacy `# file:` parser is still accepted as a fallback for CLIs
without file-write tools (e.g. some mock setups), but **bodies that look
like placeholders are rejected** before they overwrite a file:

- `…(생략)…`, `<keep previous>`, `... elided ...`, `... unchanged ...`
- Short bodies that consist only of an ellipsis

This addresses the v1 sequence where a 49 KB manuscript was wiped to 1
byte because the agent ran out of output tokens mid-emit and shipped
`"...(전체는 워크스페이스와 동일)..."` as the "new contents."

## Worker Layout

```text
Planner = R + P   →   plan.md + verify.sh
Builder = I       →   workspace files (via tools)
Critic  = V + J   →   judge.json
```

Logical phases stay RPIVJ even though the implementation has three workers.

## State Layout

```text
state/
└─ <task-id>/
   ├─ task.md          # user task (immutable)
   ├─ r.md             # R notes (subset of plan)
   ├─ plan.md          # current cycle's plan
   ├─ build.md         # builder stdout log
   ├─ check.json       # verifier result
   ├─ judge.json       # critic verdict
   ├─ state.json       # cross-cycle context (history, best_tag, hints)
   ├─ log.jsonl        # append-only event timeline
   ├─ metrics.jsonl    # token/cost/latency per phase
   ├─ checkpoints/     # per-phase snapshots
   └─ workspace/       # git repo
      ├─ .git/
      ├─ .gitignore
      ├─ verify.sh     # written by Planner
      └─ <files>       # written by Builder
```

`state.json` carries the cross-cycle context that v1 lacked:

```json
{
  "cycle": 3,
  "best_tag": "best-cycle-002",
  "last_pre_cycle_sha": "a3f...",
  "previous_action": "redo_P",
  "previous_hint": "abstract exceeded word limit",
  "history": [
    {"cycle": 1, "passed": false, "action": "redo_P", "hint": "verify shell-escape"},
    {"cycle": 2, "passed": false, "action": "redo_P", "hint": "abstract too long"}
  ]
}
```

History is capped at the most recent 8 cycles and injected into every
worker's prompt so they can learn from prior attempts without a stateful
session.

## Quick Start

```bash
python -m agent_loop_lite.cli run "write a hello world python script"
python -m agent_loop_lite.cli list
python -m agent_loop_lite.cli resume <task-id>
```

Default models are test-friendly:

```toml
[models]
planner = "mock"
builder = "mock"
critic = "rule"
```

Override per worker (use real CLIs):

```bash
agent-lite run "task" \
  --planner-model "shell:codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -" \
  --builder-model "shell:cursor-agent -p --force --model composer-2" \
  --critic-model  "shell:claude -p --dangerously-skip-permissions --model opus"
```

Supported model strings:

- `mock`: deterministic local response for tests and dry runs
- `rule`: deterministic judge for the Critic worker
- `shell:<command>`: runs a local command with the prompt on stdin
- `litellm/<model>`: optional LiteLLM provider, enabled by installing
  `agent-loop-lite[litellm]`

## Config

```toml
root = "state"
max_cycles = 3
max_redo = 2
model_timeout_s = 600

[models]
planner = "mock"
builder = "mock"
critic = "rule"

[verify]
mode = "plan"      # plan | none | pytest | shell
command = ""       # used when mode = shell
timeout_s = 30

[stop_gate]       # v2.3 — optional second-opinion before final stop
enabled = false   # off by default
model = ""        # explicit model spec; empty → use `auto`
auto = true       # pick a vendor different from `models.critic`
```

## Stop-Gate (v2.3)

After the main Critic returns ``action == "stop"``, the loop optionally
calls a *fresh-context* reviewer to look for one concrete submission
blocker. This is the cross-vendor pattern that caught a corrupted
reference list during the NMI manuscript runs (codex flagged it; the
main claude Critic and a gemini second opinion both missed it).

The gate is intentionally narrow:

```text
fresh prompt:  task.md + final plan.md + check.json + git diff
                ← no history, no previous hint, no cycle log
output:        { blocker: bool, evidence: "…", minimal_fix: "…" }
```

- ``blocker == true`` → the final judge is amended to ``redo_P`` and
  the gate's evidence + fix are appended to the hint.
- ``blocker == false`` → the stop stands; the gate event is logged.
- gate errors or invalid JSON → fail-open (the loop never breaks on a
  flaky reviewer); a ``stop_gate_error`` / ``stop_gate_invalid_json``
  event is logged for inspection.

### Configuration priority

```text
1. CLI:     --stop-gate-model "shell:…"   (or --no-stop-gate to disable)
2. TOML:    [stop_gate].model = "…"
3. Auto:    pick a different vendor from models.critic
            (claude ↔ codex, codex → claude, gemini → codex)
4. Default: disabled
```

Auto vendor selection inspects ``models.critic`` for the keywords
``claude`` / ``codex`` / ``gemini`` / ``cursor`` and routes to a
different vendor's CLI by default. Override via TOML or CLI any time:

```bash
# explicit override for one run
agent-lite run "…" --stop-gate-model "shell:gemini -p"

# disable for one run even if config enables it
agent-lite run "…" --no-stop-gate
```

```toml
# always-on with explicit model
[stop_gate]
enabled = true
model = "shell:codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -"

# always-on with auto vendor pick
[stop_gate]
enabled = true
auto = true
```

The Stop-Gate is read-only — any file it writes to workspace is reverted
via ``git reset --hard`` (same enforcement as the main Critic tier).

## Verification contract

In `mode = "plan"` the loop runs `bash verify.sh` (or `python3 verify.py`)
**automatically** if Planner produced one. This is the v2 contract — the
Planner ships a real script, not a heredoc inlined in plan.md, so:

- Re-runnable manually for debugging
- No shell escaping bugs
- Same gate every cycle until Planner updates the script

`plan.md` retains a `## Verification Strategy` section for humans, but the
script is the source of truth.

Example `verify.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
[[ -f fib.py ]] || { echo "FAIL: missing fib.py" >&2; exit 1; }
out=$(python3 fib.py)
[[ "$out" == *55* ]] || { echo "FAIL: expected '55' in stdout" >&2; exit 1; }
echo PASS
```

## Promote / Rollback

```text
PASS → git tag best-cycle-N (force-updated)
       state.best_tag = "best-cycle-N"
       redo_count = 0

FAIL → git reset --hard <state.best_tag>
       (falls back to best-cycle-0 = pre-task snapshot)
       redo_count += 1
       loop until max_redo or stop
```

PASS-only promotion (v2 policy): a failing cycle is never tagged "best."
This is intentionally simpler than v1's `better=true` heuristic; the cost
of a rare "fail-but-improving" case is small compared to the bookkeeping.

### redo_count = consecutive-same-hint streak (v2.6)

`max_redo` does **not** count every failed cycle. It counts how many
times the same judge hint repeats in a row:

```text
cycle 1 fails with hint A → redo_count = 1
cycle 2 fails with hint B → redo_count = 1  (reset — different blocker)
cycle 3 fails with hint B → redo_count = 2  (streak — same blocker)
```

This was added after a polish run where Stop-Gate caught a *different*
blocker every cycle (real progress) yet the raw-count budget cut the
loop off. Same-hint streak preserves the "stuck on the same failure"
safety net while letting incremental progress proceed up to
`max_cycles`.

## Worker-crash handling (v2.7)

Every worker (Planner / Builder / Critic) is a separate CLI subprocess.
A subprocess can:

- time out (subprocess.run hits ``model_timeout_s``)
- exit non-zero (CLI auth error, rate limit, internal bug)
- hang and leave orphan child processes

Before v2.7 these failures raised an exception that propagated up
through ``run_*`` into ``LoopRunner.run()``, killing the whole
agent-lite process and leaving ``state.json`` frozen at the dead
phase with no clean status.

v2.7 wraps each worker call in ``_safe_call``:

```text
worker call OK  → continue
worker call raises → log "worker_error" event with reason
                     state.status = "worker_error"
                     loop breaks, RunResult returned cleanly
```

Choice: a crashed worker **stops** the loop rather than advancing.
Continuing with a half-written plan / build / judgment would feed
inconsistent state to the next phase. Run ``agent-lite resume
<task-id>`` once the underlying CLI is healthy — the loop picks up
at the saved ``next_phase``.

Stop-Gate already had its own fail-open path (errors are logged as
``stop_gate_error`` and the gate is treated as "no blocker"); v2.7
brings the other three workers in line.

## Builder prompt: plan is master, hint is supplementary (v2.5)

A polish run can produce a substantive plan with many enumerated fixes
yet have the Builder only touch the one item highlighted in the prior
judge hint — LLM attention defaults to the most concrete, most recent
signal. The Builder prompt now declares:

- The **Plan** is the complete spec; address every numbered/bulleted
  item under "Mandatory Fixes" / "Implementation Steps".
- The prior judge **hint** is evidence of insufficient coverage, NOT
  a smaller assignment. Past failures don't shrink the to-do list.
- Mentally checklist the plan before declaring done; items the hint
  didn't mention still need to be handled now.

Prompt-only intervention. If Builder still narrows in real runs, the
next step would be to teach Builder to self-run `verify.sh`, but that
mixes the I and V phases and is held back for now.

## Staged Builder (optional)

For large outputs (~5,000+ words or many independent files), the Planner
can split the Implement phase into stages:

```md
## Stages
### stage 1
- subtask: polish manuscript.md (NMI format)
- subtask: polish SI.md (NMI format)

### stage 2
- subtask: cross-check references across both files
- subtask: verify cross-file consistency
```

- `### stage N` headers and `- subtask: …` bullets are parsed.
- Stages run **sequentially**.
- Subtasks within one stage run in parallel via a `ThreadPoolExecutor`,
  capped at `min(N, 4)` workers.
- Subtasks must own disjoint output files — two subtasks in the same
  stage must never overwrite the same file.
- If `plan.md` has no `### stage` headers, a single Builder call runs.

## What carries across cycles

| File / artifact            | Type        | Used by next cycle? |
|----------------------------|-------------|---------------------|
| `task.md`                  | immutable   | yes (always)        |
| `state.json` (history)     | accumulated | yes (history window)|
| `state.json` (best_tag)    | overwritten | yes (rollback target)|
| `log.jsonl`, `metrics.jsonl` | append-only | not in prompt, sits on disk for audit |
| `plan.md`, `judge.json`    | overwritten | yes (last cycle only)|
| `workspace/` (git log)     | accumulated | yes (recent commits in prompt) |

Workers are stateless processes (a fresh CLI subprocess every call), but
they receive enough cross-cycle context via `state.json` + recent
`git log` + `history` to behave as if stateful.

## Smoke test

```bash
agent-lite run "Create fib.py that prints the first 10 Fibonacci numbers. \
  Provide a verify.sh that runs the script and asserts '55' appears in stdout." \
  --task-id fib --cycles 2
```

Expected: one cycle, `check.json` shows `bash verify.sh` returncode 0,
`judge.json` action `stop`, and the workspace has a clean `git log` like:

```
cycle-001-I builder      (fib.py)
planner-cycle            (verify.sh)
init                     (.gitignore)
```
