"""Turn OneDayOnly page data into a flat list of products.

The site is built with Next.js, which puts each page's data in a JSON blob:
  - inside HTML pages, in <script id="__NEXT_DATA__"> (under "props" -> "pageProps")
  - or directly, from /_next/data/<buildId>/en-GB/<page>.json (under "pageProps")
Both contain the same "pageProps", so the rest of the code doesn't care which one we used.
"""

import json

from bs4 import BeautifulSoup

# Keys in pageProps that are not product lists (site menus, SEO tags, error tracking).
NON_PRODUCT_KEYS = {"layout", "meta", "_sentryBaggage", "_sentryTraceData"}


def next_data_from_html(html: str) -> dict:
    """Return the __NEXT_DATA__ JSON embedded in an HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag is None:
        raise ValueError("No __NEXT_DATA__ found; the site layout may have changed.")
    return json.loads(tag.string)


def extract_products(page_props: dict, default_section: str, base_url: str, fetched_at: str) -> list[dict]:
    """Find every product in a page's data and return them as clean dictionaries.

    We search the JSON recursively instead of hard-coding the exact nesting, because the
    homepage mixes "hero", "gallery" and "shop" blocks, and that layout changes often.
    A product is any dict that has both "realId" and "price".
    """
    products = []
    for key, value in page_props.items():
        if key not in NON_PRODUCT_KEYS:
            _walk(value, default_section, products)

    # The same product can appear twice on one page (e.g. a hero banner and a gallery).
    seen, unique = set(), []
    for raw, section in products:
        if raw["realId"] not in seen:
            seen.add(raw["realId"])
            unique.append(_normalise(raw, section, base_url, fetched_at))
    return unique


def _walk(node, section: str, found: list):
    """Depth-first search for product dicts, remembering which shop block we're inside."""
    if isinstance(node, dict):
        if "realId" in node and "price" in node:
            found.append((node, section))
            return  # don't look inside a product (avoids picking up variants)
        # Homepage "shop" blocks (e.g. "Russell Hobbs", "Last Chance Extra Time Deals")
        # give a more useful section name than "Today's Deals".
        if node.get("type") == "shop" and isinstance(node.get("props"), dict) and node["props"].get("name"):
            section = node["props"]["name"]
        for child in node.values():
            _walk(child, section, found)
    elif isinstance(node, list):
        for child in node:
            _walk(child, section, found)


def _money(field) -> float | None:
    """Prices look like {"value": 7999, "formattedValue": "R7,999"}. Return the number, or None."""
    if isinstance(field, dict) and field.get("value") is not None:
        return float(field["value"])
    return None


def _categories(field) -> str | None:
    """topLevelCategories can be one dict, a list of dicts, or an empty string."""
    if isinstance(field, dict):
        field = [field]
    if isinstance(field, list):
        names = [c.get("name", "") for c in field if isinstance(c, dict)]
        return "; ".join(n for n in names if n) or None
    return None


def _normalise(raw: dict, section: str, base_url: str, fetched_at: str) -> dict:
    was_price = _money(raw.get("retailPrice"))
    if was_price == 0:
        was_price = None  # a was-price of R0 means "unknown", not "free"
    return {
        "product_id": raw["realId"],                 # ODO's numeric id, stable across pages
        "name": raw.get("name"),
        "brand": raw.get("brand"),
        "category": _categories(raw.get("topLevelCategories")),
        "section": section,
        "price": _money(raw.get("price")),          # current price (cheapest option if a range)
        "was_price": was_price,                     # "retail" price shown crossed out
        "has_price_range": bool(raw.get("hasPriceRange")),
        "is_sold_out": bool(raw.get("isSoldOut")),  # listing-level only; can be wrong for variants
        "is_promo": bool(raw.get("isPromotionalDeal")),
        "deal_ends": raw.get("activeToDate") or None,
        "url": f"{base_url}/products/{raw.get('id')}",
        "fetched_at": fetched_at,
    }
