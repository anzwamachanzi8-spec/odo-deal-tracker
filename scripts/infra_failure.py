"""Count a failed GitHub Actions run where the tracker itself never started.

Called by .github/workflows/tracker.yml when an earlier step fails (e.g. installing packages,
or fetching the data repo). Uses only the standard library, because the failure may be that
our packages couldn't be installed.

Shares the consecutive_failures counter in data/run_state.json with main.py, and pushes the
same "N failed runs in a row" alert when it reaches [schedule].failure_alert_after.
"""

import json
import os
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "run_state.json"

config = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
state = json.loads(STATE.read_text()) if STATE.exists() else {}
failures = state.get("consecutive_failures", 0) + 1
state.update(consecutive_failures=failures, last_error="a workflow step failed before the tracker ran",
             hb_failed=state.get("hb_failed", 0) + 1)
STATE.parent.mkdir(exist_ok=True)
STATE.write_text(json.dumps(state, indent=2))
print(f"Failures in a row: {failures}")

if failures == config["schedule"]["failure_alert_after"] and os.environ.get("NTFY_TOPIC"):
    run_url = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    payload = {"topic": os.environ["NTFY_TOPIC"], "title": f"ODO tracker: {failures} failed runs in a row",
               "message": "A GitHub Actions step failed before the tracker could run. Tap to see the log.",
               "click": run_url, "priority": 4, "tags": ["warning"]}
    req = urllib.request.Request(config["notifications"]["ntfy"]["server"], data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)
    print("Failure alert pushed")
