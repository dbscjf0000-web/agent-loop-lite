from agent_loop_lite.jsonutil import extract_json


def test_extract_json_from_fence_with_trailing_comma():
    raw = """Here:
```json
{"action": "redo_P", "better": false,}
```
"""
    assert extract_json(raw)["action"] == "redo_P"


def test_extract_json_from_brace_slice():
    assert extract_json("prefix {\"score\": 0.5} suffix") == {"score": 0.5}
