"""Command-line entry point.

Usage (from the odo-deal-tracker folder, with the virtual environment active):
    python main.py run                  # what Task Scheduler runs every 5 min: full or fast, alerts, digest
    python main.py fetch --mode fast    # manual: today's deals only, 1 request
    python main.py fetch --mode full    # manual: every section
    python main.py deals --top 20       # best deals from the latest stored run
    python main.py deals --tier HOT     # only HOT deals (also: --suspicious)
    python main.py digest               # email the daily top-15 (once per day)
    python main.py test-notify          # send a fake deal through every channel (--dry-run to preview)
    python main.py resume               # restart after the tracker paused itself (site blocked us)
"""

import argparse
import os
import sys
import traceback
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from odo_tracker import scoring, storage
from odo_tracker import state as run_state
from odo_tracker.config import load_config
from odo_tracker.fetcher import TooSoonError, check_product_page, fetch, full_run_due
from odo_tracker.http_client import BlockedError, PoliteClient
from odo_tracker.logs import start_logging
from odo_tracker.notify import SAST, Notifier


def show_sample(products: list[dict], n: int = 10):
    """Print n products spread evenly across the list, so several sections show up."""
    step = max(1, len(products) // n)
    print(f"\n{'Name':<42} {'Brand':<18} {'Section':<22} {'Price':>8} {'Was':>8}  Category")
    print("-" * 125)
    for p in products[::step][:n]:
        price = f"R{p['price']:,.0f}" if p["price"] is not None else "-"
        was = f"R{p['was_price']:,.0f}" if p["was_price"] is not None else "-"
        print(f"{p['name'][:41]:<42} {(p['brand'] or '')[:17]:<18} {p['section'][:21]:<22} "
              f"{price:>8} {was:>8}  {p['category'] or '-'}")


def show_changes(summary: dict, limit: int = 10):
    """Print counts of new / removed / price-changed items, and a few examples of each."""
    if summary["first_run"]:
        print("\nFirst run of this mode: nothing to compare with yet.")
        return
    changes = summary["changes"]
    for kind in ("new", "returned", "removed", "price_change"):
        rows = [c for c in changes if c["change_type"] == kind]
        print(f"\n{kind.replace('_', ' ').title()}: {len(rows)}")
        for c in rows[:limit]:
            if kind == "price_change":
                print(f"  {c['name'][:50]:<50} {_rand(c['old_price'])} -> {_rand(c['new_price'])}"
                      f"  (was-price {_rand(c['old_was_price'])} -> {_rand(c['new_was_price'])})")
            else:
                print(f"  {c['name'][:50]:<50} {_rand(c['old_price'] if kind == 'removed' else c['new_price'])}")
        if len(rows) > limit:
            print(f"  ... and {len(rows) - limit} more")


def _rand(value) -> str:
    return f"R{value:,.0f}" if value is not None else "-"


def show_tier_counts(deals: list[dict]):
    counts = {t: sum(d["tier"] == t for d in deals) for t in ("URGENT", "HOT", "NORMAL")}
    flagged = sum(d["suspicious"] for d in deals)
    print(f"Tiers: URGENT {counts['URGENT']} | HOT {counts['HOT']} | NORMAL {counts['NORMAL']}"
          f"   (suspicious was-price: {flagged})")


def show_deals(deals: list[dict]):
    print(f"\n{'Tier':<7}{'Off':>5}  {'Name':<44}{'Price':>8}{'Was':>9}  {'Section':<20} Flags")
    print("-" * 130)
    for d in deals:
        off = f"{d['discount']:.0%}" if d["discount"] is not None else "?"
        flags = ", ".join(("!" if f in scoring.TRUST_FLAGS else "") + f for f in d["flags"])
        print(f"{d['tier']:<7}{off:>5}  {d['name'][:43]:<44}{_rand(d['price']):>8}{_rand(d['was_price']):>9}"
              f"  {d['section'][:19]:<20} {flags}")


def _db(config):
    return closing(storage.connect(config["paths"]["data_dir"] / config["paths"]["database"]))


# --- fetching ---------------------------------------------------------------------------

def run_fetch(config: dict, mode: str, verbose: bool):
    """Fetch, store a snapshot, score, and send alerts. verbose=True prints a sample and details."""
    client = PoliteClient(config["politeness"])
    products, started_at = fetch(config, mode, client)
    if verbose:
        show_sample(products)
        print(f"\nTotal items: {len(products)}")
        print(f"Missing current price: {sum(p['price'] is None for p in products)}")
        print(f"Missing was-price:     {sum(p['was_price'] is None for p in products)}")

    with _db(config) as conn:
        counts_before = storage.content_counts(conn)
        summary = storage.save_run(conn, mode, products, started_at)
        deals = scoring.score_run(conn, summary["run_id"], config["scoring"])
        if verbose:
            show_changes(summary)
        else:
            counts = {k: sum(c["change_type"] == k for c in summary["changes"])
                      for k in ("new", "returned", "removed", "price_change")}
            print(f"Run #{summary['run_id']}: {len(products)} items; changes {counts}")
        show_tier_counts(deals)
        sent = Notifier(config).alert_new_deals(conn, deals, confirm=lambda url: check_product_page(client, url))
        print(f"Alerts: URGENT sent {sent['URGENT']}, HOT sent {sent['HOT']}, skipped {sent['skipped']}")
        # New product versions, changes or alerts = data worth saving (used by the cloud version).
        data_changed = storage.content_counts(conn) != counts_before
    return {"alerts": sent["URGENT"] + sent["HOT"], "data_changed": data_changed}


def cmd_fetch(args):
    config = load_config()
    print(f"Fetching ({args.mode})...")
    run_fetch(config, args.mode, verbose=True)


# --- scheduled run ----------------------------------------------------------------------

def cmd_run(args):
    """One scheduled tick. Task Scheduler starts this every 5 minutes.

    A single task (instead of separate fast and full tasks) means the two can never collide:
    each tick does a full run if 30 minutes have passed, otherwise a fast check. The full run
    includes today's deals, so the URGENT check still happens every 5 minutes.
    """
    config = load_config()
    start_logging(config["paths"]["log_dir"])
    notifier = Notifier(config)
    state = run_state.load(config)

    if state.get("paused_reason"):
        print(f"Paused since {state['paused_at']}: {state['paused_reason']}. "
              f"Check the site in a browser, then run: python main.py resume")
        return

    mode = "full" if full_run_due(config) else "fast"
    print(f"=== {mode} run ===")
    try:
        result = run_fetch(config, mode, verbose=False)
        if mode == "full":
            send_digest_if_due(config, notifier)
    except TooSoonError as err:
        print(err)  # respecting the rate limit is not a failure
        return
    except BlockedError as err:
        # Per the project rules: stop instead of working around a block.
        _update_state(config, paused_reason=str(err), paused_at=_now())
        notifier.push("ODO tracker PAUSED: site blocked us",
                      f"{err}\nThe tracker has stopped making requests. Check the site in a browser, "
                      f"then resume it (see README).", priority=4, tags=["no_entry"])
        print(f"BLOCKED: {err}. Tracker paused.")
        _mark_data_changed()  # save the pause, so it survives in the cloud
        sys.exit(2)
    except Exception as err:
        _record_failure(config, notifier, err)
        sys.exit(1)
    else:
        _record_success(config, notifier, result["alerts"])
        if result["data_changed"]:
            _mark_data_changed()
        send_heartbeat_if_due(config, notifier)


def _mark_data_changed():
    """Tell the GitHub Actions workflow to save the database (it sets ODO_CHANGED_FLAG to a file path).

    Does nothing on the laptop, where the database file is simply kept on disk.
    """
    flag = os.environ.get("ODO_CHANGED_FLAG")
    if flag:
        Path(flag).touch()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _update_state(config, **changes) -> dict:
    # Reload first: fetch() writes the run times into the same file during the run.
    state = run_state.load(config)
    state.update(changes)
    run_state.save(config, state)
    return state


def _record_failure(config, notifier, err):
    print("RUN FAILED:\n" + traceback.format_exc())
    state = run_state.load(config)
    failures = state.get("consecutive_failures", 0) + 1
    _update_state(config, consecutive_failures=failures, last_error=f"{type(err).__name__}: {err}",
                  hb_failed=state.get("hb_failed", 0) + 1)
    threshold = config["schedule"]["failure_alert_after"]
    if failures == threshold:  # alert once, not on every failure after that
        notifier.push(f"ODO tracker: {failures} failed runs in a row",
                      f"Last error: {type(err).__name__}: {err}\nSee the log (logs/tracker.log, "
                      f"or the run's log on GitHub)", priority=4, tags=["warning"])


def _record_success(config, notifier, alerts_sent: int):
    state = run_state.load(config)
    failures = state.get("consecutive_failures", 0)
    if failures >= config["schedule"]["failure_alert_after"]:
        notifier.push("ODO tracker recovered", f"Working again after {failures} failed runs.", tags=["white_check_mark"])
    # Counters for the daily heartbeat.
    _update_state(config, consecutive_failures=0, hb_runs=state.get("hb_runs", 0) + 1,
                  hb_alerts=state.get("hb_alerts", 0) + alerts_sent)


# --- heartbeat --------------------------------------------------------------------------

def send_heartbeat_if_due(config, notifier) -> None:
    """Once a day after [schedule].heartbeat_time (SAST), push a short "still running" note.

    Most days nothing alert-worthy happens, so a quiet phone can't tell you the tracker is alive.
    If this push doesn't arrive, something is wrong (laptop asleep, GitHub disabled the workflow...).
    """
    sched = config["schedule"]
    now = datetime.now(SAST)
    today = now.date().isoformat()
    state = run_state.load(config)
    if not sched.get("heartbeat", True) or state.get("last_heartbeat_date") == today:
        return
    if now.strftime("%H:%M") < sched["heartbeat_time"]:
        return
    since = state.get("hb_since")
    period = f"since {since}" if since else "since the tracker started"
    text = (f"{state.get('hb_runs', 0)} successful runs, {state.get('hb_failed', 0)} failed, "
            f"{state.get('hb_alerts', 0)} deal alerts {period}.")
    if notifier.push("ODO tracker is running", text, priority=2, tags=["heartbeat"]):
        print(f"Heartbeat sent: {text}")
        _update_state(config, last_heartbeat_date=today, hb_runs=0, hb_failed=0, hb_alerts=0,
                      hb_since=now.strftime("%a %H:%M"))


# --- digest -----------------------------------------------------------------------------

def send_digest_if_due(config, notifier, force=False) -> None:
    """Send the daily digest once per day, after [schedule].digest_time (SAST)."""
    now = datetime.now(SAST)
    today = now.date().isoformat()
    if not force:
        if run_state.load(config).get("last_digest_date") == today:
            return
        if now.strftime("%H:%M") < config["schedule"]["digest_time"]:
            return
    with _db(config) as conn:
        run_id = storage.latest_run_id(conn, "full")
        if run_id is None:
            print("Digest: no full runs stored yet.")
            return
        deals = scoring.score_run(conn, run_id, config["scoring"])
    print("Sending daily digest...")
    if notifier.send_digest(deals):
        _update_state(config, last_digest_date=today)


def cmd_digest(args):
    """Manual digest. Without --force it only sends if it's past digest_time and not sent today."""
    config = load_config()
    send_digest_if_due(config, Notifier(config, dry_run=args.dry_run), force=args.force)


# --- other commands ---------------------------------------------------------------------

def fake_deal(tier: str) -> dict:
    """A made-up deal for testing channels. The URL is the real homepage so the link works."""
    price, was = (0.0, 499.0) if tier == "URGENT" else (149.0, 999.0)
    deal = {"product_id": -1, "name": f"TEST {tier} deal – Example Bluetooth Speaker", "brand": "TestBrand",
            "category": "electronics", "section": "Today's Deals", "price": price, "was_price": was,
            "has_price_range": False, "is_sold_out": False, "is_promo": False, "deal_ends": None,
            "url": "https://www.onedayonly.co.za/", "fetched_at": ""}
    return scoring.score(deal, None, {"urgent_discount": 1.0, "hot_discount": 0.7,
                                      "was_price_jump_factor": 1.25, "was_price_vs_seen_price_factor": 5.0})


def cmd_test_notify(args):
    """Send a fake URGENT alert, a fake HOT alert and a fake digest. Doesn't touch the site or database."""
    config = load_config()
    notifier = Notifier(config, dry_run=args.dry_run)
    urgent, hot = fake_deal("URGENT"), fake_deal("HOT")
    print("URGENT test (push + email + WhatsApp if enabled):")
    notifier.send_urgent(urgent)
    print("HOT test (push + WhatsApp if enabled):")
    notifier.send_hot(hot)
    print("Digest test (email):")
    notifier.send_digest([urgent, hot])


def cmd_deals(args):
    """Score the latest stored run (no requests to the site) and show the best deals."""
    config = load_config()
    with _db(config) as conn:
        run_id = storage.latest_run_id(conn, args.mode)
        if run_id is None:
            print(f"No {args.mode} runs stored yet. Run: python main.py fetch --mode {args.mode}")
            return
        deals = scoring.score_run(conn, run_id, config["scoring"])
    print(f"Run #{run_id} ({args.mode}), {len(deals)} items")
    show_tier_counts(deals)
    if args.tier:
        deals = [d for d in deals if d["tier"] == args.tier]
    if args.suspicious:
        deals = [d for d in deals if d["suspicious"]]
    show_deals(deals[:args.top])
    print("\nFlags starting with ! mean the discount may not be trustworthy.")


def cmd_resume(args):
    config = load_config()
    state = run_state.load(config)
    for key in ("paused_reason", "paused_at"):
        state.pop(key, None)
    state["consecutive_failures"] = 0
    run_state.save(config, state)
    print("Resumed. The next scheduled run will fetch again.")


def main():
    parser = argparse.ArgumentParser(description="OneDayOnly deal tracker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="one scheduled tick: full or fast run, alerts, digest, logging")
    f = sub.add_parser("fetch", help="fetch products, save a snapshot, show what changed")
    f.add_argument("--mode", choices=["fast", "full"], default="full")
    d = sub.add_parser("deals", help="rank the latest stored run by discount (no site requests)")
    d.add_argument("--mode", choices=["fast", "full"], default="full")
    d.add_argument("--top", type=int, default=20, help="how many deals to show")
    d.add_argument("--tier", choices=["URGENT", "HOT", "NORMAL"], help="only show this tier")
    d.add_argument("--suspicious", action="store_true", help="only show flagged deals")
    g = sub.add_parser("digest", help="email the top deals from the latest full run")
    g.add_argument("--force", action="store_true", help="send now, even if already sent today")
    g.add_argument("--dry-run", action="store_true", help="print instead of sending")
    t = sub.add_parser("test-notify", help="send a fake deal through every enabled channel")
    t.add_argument("--dry-run", action="store_true", help="print instead of sending")
    sub.add_parser("resume", help="restart after the tracker paused itself")
    args = parser.parse_args()

    commands = {"run": cmd_run, "fetch": cmd_fetch, "deals": cmd_deals, "digest": cmd_digest,
                "test-notify": cmd_test_notify, "resume": cmd_resume}
    try:
        commands[args.command](args)
    except TooSoonError as err:
        print(err)          # not an error: we're just respecting the rate limit
    except BlockedError as err:
        print(f"BLOCKED: {err}\nStopping. Don't retry; check the site manually.")
        sys.exit(2)


if __name__ == "__main__":
    main()
