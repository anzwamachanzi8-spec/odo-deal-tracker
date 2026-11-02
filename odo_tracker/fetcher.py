"""Fetch products from OneDayOnly.

fast check: 1 request (the homepage = today's deals). Used for the URGENT check.
full run:   the homepage + every page in config [site].full_run_pages.
"""

from datetime import datetime, timedelta, timezone

from . import state as run_state
from .http_client import PoliteClient
from .parser import extract_products, next_data_from_html

TODAY_SECTION = "Today's Deals"


class TooSoonError(Exception):
    """A run was started before the minimum interval in config had passed."""


def fetch(config: dict, mode: str, client: PoliteClient | None = None) -> tuple[list[dict], str]:
    """Run a 'fast' or 'full' fetch. Returns (list of product dicts, run start time in UTC).

    Pass in a client if you'll make more requests afterwards (e.g. a product-page check),
    so the 2-3 second pause between requests still applies.
    """
    site = config["site"]
    _check_rate_limit(config, mode)

    client = client or PoliteClient(config["politeness"])
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    base = site["base_url"]

    # 1) Homepage as HTML. Besides today's deals, it gives us the Next.js buildId,
    #    which the JSON URLs for the other pages need.
    resp = client.get(f"{base}/")
    resp.raise_for_status()
    next_data = next_data_from_html(resp.text)
    build_id = next_data["buildId"]
    products = extract_products(next_data["props"]["pageProps"], TODAY_SECTION, base, fetched_at)
    print(f"  {TODAY_SECTION}: {len(products)} items")

    if mode == "full":
        seen = {p["product_id"] for p in products}
        for page in site["full_run_pages"]:
            page_props = _fetch_page_props(client, base, build_id, page["path"])
            items = extract_products(page_props, page["section"], base, fetched_at)
            # An item can be on two pages (e.g. Last Chance shop on the homepage AND Extra Time).
            # Keep the first one we saw so each product appears once per run.
            new = [p for p in items if p["product_id"] not in seen]
            seen.update(p["product_id"] for p in new)
            products.extend(new)
            print(f"  {page['section']}: {len(items)} items ({len(items) - len(new)} already seen)")

    print(f"  ({client.request_count} requests)")
    return products, fetched_at


def check_product_page(client: PoliteClient, url: str) -> dict:
    """Open one product page and return its current {"price", "is_sold_out"}.

    The product page is more up to date than listings: on 2026-11-01 a listing still said
    "in stock" while every colour on the product page was sold out.
    """
    resp = client.get(url)
    resp.raise_for_status()
    product = next_data_from_html(resp.text)["props"]["pageProps"]["product"]
    price = product.get("price") or {}
    return {"price": price.get("value"), "is_sold_out": bool(product.get("isSoldOut"))}


def _fetch_page_props(client: PoliteClient, base: str, build_id: str, path: str) -> dict:
    """Get a page's data from the lighter JSON endpoint; fall back to the HTML page.

    The JSON URL contains the buildId, which changes whenever ODO deploys a new version
    of the site. If it changed since we read the homepage, the JSON URL returns 404.
    """
    resp = client.get(f"{base}/_next/data/{build_id}/en-GB{path}.json")
    if resp.status_code == 200:
        return resp.json()["pageProps"]
    if resp.status_code != 404:
        resp.raise_for_status()
    print(f"  JSON for {path} returned 404 (site probably redeployed); using the HTML page")
    resp = client.get(f"{base}{path}")
    resp.raise_for_status()
    return next_data_from_html(resp.text)["props"]["pageProps"]


# --- rate limiting between runs -------------------------------------------------------
# We remember when each kind of run last started in data/run_state.json. Both run types
# request the homepage, so a fast check is also blocked for 5 minutes after a full run.

def _check_rate_limit(config: dict, mode: str):
    state = run_state.load(config)
    now = datetime.now(timezone.utc)
    p = config["politeness"]
    grace = timedelta(seconds=p.get("schedule_grace_seconds", 0))

    def started_within(key: str, minutes: float) -> bool:
        last = state.get(key)
        return last is not None and now - datetime.fromisoformat(last) < timedelta(minutes=minutes) - grace

    if mode == "full" and started_within("last_full", p["full_run_min_interval_minutes"]):
        raise TooSoonError(f"Full run skipped: the last one started at {state['last_full']} "
                           f"(minimum gap {p['full_run_min_interval_minutes']} min).")
    for key in ("last_fast", "last_full"):
        if started_within(key, p["fast_check_min_interval_minutes"]):
            raise TooSoonError(f"{mode} run skipped: the homepage was requested at {state[key]} "
                               f"(minimum gap {p['fast_check_min_interval_minutes']} min).")

    # Record the start time now, before any request, so even a failed run counts.
    state[f"last_{mode}"] = now.isoformat(timespec="seconds")
    run_state.save(config, state)


def full_run_due(config: dict) -> bool:
    """True if at least the minimum full-run gap has passed since the last full run."""
    last = run_state.load(config).get("last_full")
    if last is None:
        return True
    p = config["politeness"]
    gap = timedelta(minutes=p["full_run_min_interval_minutes"]) - timedelta(seconds=p.get("schedule_grace_seconds", 0))
    return datetime.now(timezone.utc) - datetime.fromisoformat(last) >= gap
