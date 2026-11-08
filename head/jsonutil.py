"""Small helpers for reading JSON out of model replies."""

import json


def parse_json(raw: str) -> dict:
    """The JSON object in a model reply (tolerates prose or code fences around it). Raises ValueError."""
    if "{" not in raw or "}" not in raw:
        raise ValueError(f"no JSON object in reply {raw[:120]!r}")
    return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
