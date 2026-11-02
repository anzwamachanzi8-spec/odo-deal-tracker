"""Small JSON file (data/run_state.json) that remembers things between runs:
when each run type last started, when the digest was last sent, how many runs failed in a row,
and whether the tracker is paused because the site blocked it.
"""

import json


def _path(config: dict):
    return config["paths"]["data_dir"] / "run_state.json"


def load(config: dict) -> dict:
    path = _path(config)
    return json.loads(path.read_text()) if path.exists() else {}


def save(config: dict, state: dict):
    _path(config).write_text(json.dumps(state, indent=2))
