from __future__ import annotations

import json
import re
from typing import Any


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Extract one JSON object from a model response.

    This is deliberately small: strict parse, fenced block, then brace slice.
    A tiny trailing-comma cleanup is included because it is common in model
    output and costs little complexity.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty JSON response")

    for candidate in _candidates(raw):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        try:
            return json.loads(_strip_trailing_commas(candidate))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"could not parse JSON from response len={len(raw)}")


def _candidates(raw: str) -> list[str]:
    out = [raw]
    match = _JSON_FENCE_RE.search(raw)
    if match:
        out.append(match.group(1).strip())
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        out.append(raw[start : end + 1])
    return out


def _strip_trailing_commas(raw: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", raw)
