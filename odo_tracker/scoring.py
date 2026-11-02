"""Score deals by discount, put them in tiers, and flag ones whose was-price looks untrustworthy.

Discount % = (was_price - price) / was_price
Tiers (thresholds in config [scoring]):
    URGENT  100% off, or price is R0
    HOT     70% off or more
    NORMAL  everything else

Suspicious deals are flagged, never hidden, so you can judge them yourself.
"""

import sqlite3
from statistics import median

# Flags that mean "don't trust this discount".
TRUST_FLAGS = {"no_was_price", "free_without_was_price", "price_not_below_was",
               "was_price_jumped", "was_price_far_above_seen_prices"}
# Flags that are just useful to know.
INFO_FLAGS = {"promo", "price_range", "sold_out"}


def discount(price: float | None, was_price: float | None) -> float | None:
    """Return the discount as a fraction (0.7 = 70% off), or None if it can't be worked out."""
    if price is None or not was_price:
        return None
    return (was_price - price) / was_price


def score(product: dict, history: dict | None, cfg: dict) -> dict:
    """Score one product.

    history: what earlier runs saw for this item, {"prices": [...], "was_prices": [...]},
             or None if it has never been seen before.
    Returns a copy of the product with 'discount', 'tier', 'flags' and 'suspicious' added.
    """
    price, was = product["price"], product["was_price"]
    d = discount(price, was)
    flags = []

    # --- trust checks on the was-price ---
    if was is None:
        flags.append("free_without_was_price" if price == 0 else "no_was_price")
    elif price is not None and price >= was:
        flags.append("price_not_below_was")

    history = history or {"prices": [], "was_prices": []}
    if was is not None:
        past_was = [w for w in history["was_prices"] if w]
        if past_was and was > cfg["was_price_jump_factor"] * median(past_was):
            flags.append("was_price_jumped")
        # Highest price we've ever seen it actually sell for, including right now.
        # R0 freebies are ignored here; they're covered by the URGENT tier and the promo flag.
        seen_prices = [p for p in history["prices"] + [price] if p]
        if seen_prices and was > cfg["was_price_vs_seen_price_factor"] * max(seen_prices):
            flags.append("was_price_far_above_seen_prices")

    # --- extra info ---
    if product["is_promo"]:
        flags.append("promo")        # e.g. R0 promo freebie, usually limited to 1 per customer
    if product["has_price_range"]:
        flags.append("price_range")  # "From R...": the discount only applies to the cheapest option
    if product["is_sold_out"]:
        flags.append("sold_out")

    # --- tier ---
    if price == 0 or (d is not None and d >= cfg["urgent_discount"]):
        tier = "URGENT"
    elif d is not None and d >= cfg["hot_discount"]:
        tier = "HOT"
    else:
        tier = "NORMAL"

    return {**product, "discount": d, "tier": tier, "flags": flags,
            "suspicious": any(f in TRUST_FLAGS for f in flags)}


def rank(deals: list[dict]) -> list[dict]:
    """Sort by discount, biggest first. Items without a discount go to the bottom."""
    return sorted(deals, key=lambda d: (d["discount"] is not None, d["discount"] or 0), reverse=True)


def score_run(conn: sqlite3.Connection, run_id: int, cfg: dict) -> list[dict]:
    """Score every product in one stored run, using earlier runs as each item's history."""
    products = [dict(row) for row in conn.execute("SELECT * FROM snapshots WHERE run_id = ?", (run_id,))]
    history = load_history(conn, before_run_id=run_id)
    scored = [score(p, history.get(_key(p)), cfg) for p in products if p["price"] is not None]
    return rank(scored)


def load_history(conn: sqlite3.Connection, before_run_id: int) -> dict:
    """Map (brand, name) -> {"prices": [...], "was_prices": [...]} from runs before before_run_id.

    We match items by brand + name rather than product_id, because ODO can relist the same item
    on a later day as a new listing with a new id. DISTINCT keeps the query small: an item seen
    at the same price in 300 runs contributes one row.
    """
    history = {}
    rows = conn.execute("""SELECT DISTINCT brand, name, price, was_price FROM snapshots
                           WHERE run_id < ?""", (before_run_id,))
    for row in rows:
        h = history.setdefault(_key(row), {"prices": [], "was_prices": []})
        h["prices"].append(row["price"])
        h["was_prices"].append(row["was_price"])
    return history


def _key(item) -> tuple:
    return ((item["brand"] or "").strip().lower(), (item["name"] or "").strip().lower())
