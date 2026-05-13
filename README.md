# agent-loop-lite

`agent-loop-lite` is a small reference implementation of the RPIVJ loop:

```text
R = Read       task, prior feedback, success criteria
P = Plan       concrete next implementation plan
I = Implement  write files into workspace/
V = Validate   run deterministic checks from plan.md
J = Judge      promote best, roll back regressions, decide stop/redo
```

The design keeps the core ideas from the full agent-loop project:

- stateless workers
- file-based state as the source of truth
- RPIVJ checkpoints
- verifier-driven pass/fail
- judge-driven redo
- best snapshot and rollback
- detailed judge hints fed into the next plan
- worker-specific model selection
- optional staged Builder (parallel subtasks within sequential stages)
  for large outputs — opt-in via `### stage` headers in `plan.md`

## Worker Layout

```text
Planner = R + P
Builder = I
Critic  = V + J
```

Logical phases remain RPIVJ even though the implementation only has three
workers.

## State Layout

```text
.agent_loop/
└─ <task-id>/
   ├─ task.md
   ├─ r.md
   ├─ plan.md
   ├─ build.md
   ├─ check.json
   ├─ judge.json
   ├─ state.json
   ├─ log.jsonl
   ├─ metrics.jsonl
   ├─ checkpoints/
   └─ workspace/
      ├─ ...
      └─ best/
         ├─ manifest.json
         └─ ...
```

## Quick Start

```bash
python -m agent_loop_lite.cli run "write a hello world python script"
python -m agent_loop_lite.cli list
python -m agent_loop_lite.cli resume <task-id>
```

The default models are test-friendly:

```toml
[models]
planner = "mock"
builder = "mock"
critic = "rule"
```

Planner writes a detailed plan with success criteria and verification strategy.
Critic runs those checks, reads the workspace snapshot, and writes a detailed
next-cycle hint when it does not stop.

```text
Planner: task.md + prior judge hint -> r.md + plan.md
Builder: plan.md -> workspace/*
Critic:  plan.md + workspace/* + check result -> check.json + judge.json
```

Override them per worker:

```bash
agent-lite run "task" \
  --planner-model "litellm/openai/gpt-4o-mini" \
  --builder-model "shell:my-builder-command" \
  --critic-model "rule"
```

Supported model strings:

- `mock`: deterministic local response for tests and dry runs
- `rule`: deterministic judge for the Critic worker
- `shell:<command>`: runs a local command with the prompt on stdin
- `litellm/<model>`: optional LiteLLM provider, enabled by installing
  `agent-loop-lite[litellm]`

## Config

```toml
root = ".agent_loop"
max_cycles = 3
max_redo = 2

[models]
planner = "mock"
builder = "mock"
critic = "rule"

[verify]
mode = "plan"      # plan | none | pytest | shell
command = ""       # used when mode = shell
timeout_s = 30
```

`plan.md` is the contract:

```md
## Success Criteria
- [ ] criterion: empty input returns an empty list
  verify: `pytest tests/test_processor.py::test_empty_input -q`

## Verification Strategy
1. Run `pytest tests/test_processor.py -q`

## Redo Guidance
- If an edge-case test fails, revise the implementation plan before rebuilding.
```

## Staged Builder (optional)

For large outputs (expected total > ~5,000 words or many independent files),
the Planner can split the Implement phase into stages:

```md
## Stages
### stage 1
- subtask: polish manuscript.md (NMI format)
- subtask: polish SI.md (NMI format)

### stage 2
- subtask: cross-check references across both files
- subtask: verify cross-file consistency
```

- The loop parses `### stage N` headers and `- subtask: …` bullets.
- Stages run **sequentially**; subtasks inside a stage run **in parallel**
  via a `ThreadPoolExecutor`, each as an independent Builder call.
- Subtasks must own disjoint output files — two subtasks in the same stage
  must never overwrite the same file. The Critic relies on this.
- The configured Builder model is used for every subtask; per-subtask model
  override is deliberately not implemented to keep the scheduler small.
- If `plan.md` contains **no** `### stage` headers, the legacy single-call
  Builder path runs untouched.
