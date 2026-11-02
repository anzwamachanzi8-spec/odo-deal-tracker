"""One-off check: can a GitHub Actions runner reach OneDayOnly without being blocked?

Phase 7 step 1. Cloud servers (like GitHub's runners on Azure) are often treated differently
by Cloudflare than a home connection, so we test before building anything on top.

Makes exactly 2 requests, with the same User-Agent and pauses as the tracker:
  1. the homepage (HTML), which the fast check uses
  2. one small JSON page, which full runs use
It does not touch the database or the run state, and sends no alerts.

Exit code: 0 = reachable, 2 = blocked (403 / Cloudflare challenge), 1 = other error.
Run locally too:  python scripts/reachability_test.py
"""

import sys
from pathlib import Path

# Let this script import odo_tracker when run as `python scripts/reachability_test.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odo_tracker.config import load_config  # noqa: E402
from odo_tracker.http_client import BlockedError, PoliteClient  # noqa: E402
from odo_tracker.parser import extract_products, next_data_from_html  # noqa: E402

JSON_TEST_PATH = "/shops/everyday-essentials"  # small page (~50 items)


def main() -> int:
    config = load_config()
    base = config["site"]["base_url"]
    client = PoliteClient(config["politeness"])

    try:
        resp = client.get(f"{base}/")
        print(f"Homepage: HTTP {resp.status_code}, {len(resp.content):,} bytes, "
              f"server={resp.headers.get('server')}, cf-ray={resp.headers.get('cf-ray')}")
        resp.raise_for_status()
        next_data = next_data_from_html(resp.text)
        items = extract_products(next_data["props"]["pageProps"], "Today's Deals", base, "test")
        print(f"  parsed {len(items)} products from today's deals")

        build_id = next_data["buildId"]
        resp = client.get(f"{base}/_next/data/{build_id}/en-GB{JSON_TEST_PATH}.json")
        print(f"JSON page: HTTP {resp.status_code}, {len(resp.content):,} bytes")
        resp.raise_for_status()
        items = extract_products(resp.json()["pageProps"], "test", base, "test")
        print(f"  parsed {len(items)} products from {JSON_TEST_PATH}")
    except BlockedError as e:
        print(f"BLOCKED: {e}")
        return 2
    except Exception as e:  # anything else: network error, page layout changed, ...
        print(f"ERROR: {type(e).__name__}: {e}")
        return 1

    print("RESULT: reachable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
