from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


def call_model(
    worker: str,
    prompt: str,
    model: str,
    *,
    workspace: Path,
    timeout_s: int = 600,
) -> ModelResponse:
    started = time.monotonic()
    if model == "mock":
        text = _mock_response(worker)
    elif model == "rule":
        text = _rule_response()
    elif model.startswith("shell:"):
        text = _call_shell(model.removeprefix("shell:"), prompt, workspace, timeout_s)
    elif model.startswith("litellm/"):
        text = _call_litellm(model.removeprefix("litellm/"), prompt)
    else:
        raise RuntimeError(
            f"unsupported model '{model}'. Use mock, rule, shell:<cmd>, or litellm/<model>."
        )
    latency = time.monotonic() - started
    return ModelResponse(
        text=text,
        model=model,
        prompt_tokens=max(1, len(prompt) // 4),
        completion_tokens=max(0, len(text) // 4),
        latency_s=round(latency, 4),
    )


def _mock_response(worker: str) -> str:
    if worker == "planner":
        return (
            "# Plan\n\n"
            "## Task Summary\n"
            "Create a tiny Python output file that can be syntax-checked.\n\n"
            "## Assumptions\n"
            "- None\n\n"
            "## Success Criteria\n"
            "- [ ] criterion: `solution.py` exists and is valid Python.\n"
            "  verify: `python -m py_compile solution.py`\n\n"
            "## Files to Create or Modify\n"
            "- `solution.py` - primary implementation output\n\n"
            "## Implementation Steps\n"
            "1. Create solution.py.\n"
            "2. Keep the implementation simple.\n"
            "3. Run the verification command.\n\n"
            "## Verification Strategy\n"
            "1. Run `python -m py_compile solution.py`\n\n"
            "## Redo Guidance\n"
            "- If py_compile fails, revise the implementation in the next plan.\n"
        )
    if worker == "builder":
        return (
            "Created the primary output.\n\n"
            "```python\n"
            "# file: solution.py\n"
            "def main():\n"
            "    return 'ok'\n\n"
            "if __name__ == '__main__':\n"
            "    print(main())\n"
            "```\n"
        )
    return _rule_response()


def _rule_response() -> str:
    return (
        '{"passed": true, "better": true, "action": "stop", '
        '"hint": "", "reason": "rule-based decision"}'
    )


def _call_shell(command: str, prompt: str, workspace: Path, timeout_s: int) -> str:
    proc = subprocess.run(
        command,
        input=prompt,
        cwd=workspace,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-2000:]
        raise RuntimeError(f"shell model failed rc={proc.returncode}: {tail}")
    return proc.stdout.rstrip("\n")


def _call_litellm(model: str, prompt: str) -> str:
    try:
        import litellm
    except ImportError as exc:
        raise RuntimeError("litellm model selected but litellm is not installed") from exc

    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    return response.choices[0].message.content or ""
