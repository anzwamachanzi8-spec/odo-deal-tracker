"""Decide what to alert about, format the messages, and send them.

URGENT  ntfy push (priority 5) + email (+ WhatsApp if enabled)
HOT     ntfy push (+ WhatsApp if enabled); many at once are batched into one summary push
Digest  one email a day with the top deals by discount

An alert is recorded in the database (so it's never repeated at the same price) only after
at least one real channel delivered it. If a channel isn't set up in .env yet, the message
is printed to the console instead, and nothing is recorded, so you still get it once set up.
"""

import html
from datetime import datetime, timedelta, timezone

from . import channels, storage
from .channels import NotConfigured

# South Africa has no daylight saving, so a fixed UTC+2 offset is exact (and avoids needing
# the tzdata package on Windows).
SAST = timezone(timedelta(hours=2), "SAST")


def _rand(value) -> str:
    return f"R{value:,.0f}" if value is not None else "?"


def _off(deal) -> str:
    return f"{deal['discount']:.0%} off" if deal["discount"] is not None else "discount unknown"


def describe(deal: dict) -> str:
    """Multi-line message body for one deal."""
    lines = [f"{_rand(deal['price'])} ({_off(deal)}, was {_rand(deal['was_price'])})",
             f"{deal['brand'] or ''} · {deal['section']}".strip(" ·")]
    if "promo" in deal["flags"]:
        lines.append("PROMO item: usually 1 per customer, shipping not included")
    if "price_range" in deal["flags"]:
        lines.append("Price is for the cheapest option")
    if deal["suspicious"]:
        lines.append("⚠ Check the was-price: " + ", ".join(f for f in deal["flags"] if f not in ("promo", "price_range", "sold_out")))
    return "\n".join(lines)


class Notifier:
    def __init__(self, config: dict, dry_run: bool = False):
        self.cfg = config["notifications"]
        self.dry_run = dry_run

    # --- delivery helpers -------------------------------------------------------------

    def _deliver(self, channel: str, send, *args, **kwargs) -> bool:
        """Call one sender. Returns True only if the message really went out."""
        if not self.cfg[channel]["enabled"]:
            return False
        if self.dry_run:
            print(f"  [dry-run {channel}] {args[0] if args else ''}")
            return False
        try:
            send(self.cfg[channel], *args, **kwargs)
            print(f"  sent via {channel}")
            return True
        except NotConfigured as err:
            print(f"  [{channel} not set up: {err}] message was: {args[0] if args else ''}")
        except Exception as err:  # one broken channel must not stop the others
            print(f"  ! {channel} failed: {type(err).__name__}: {err}")
        return False

    def push(self, title, message, url=None, priority=3, tags=None) -> bool:
        return self._deliver("ntfy", channels.send_ntfy, title, message, url=url, priority=priority, tags=tags)

    def email(self, subject, text, html_body=None) -> bool:
        return self._deliver("email", channels.send_email, subject, text, html_body)

    def whatsapp(self, text, tier) -> bool:
        if tier not in self.cfg["whatsapp"]["tiers"]:
            return False
        return self._deliver("whatsapp", channels.send_whatsapp, text)

    # --- deal alerts ------------------------------------------------------------------

    def send_urgent(self, deal: dict) -> bool:
        title = f"URGENT {_off(deal)}: {deal['name']}"
        body = describe(deal)
        results = [
            self.push(title, body, url=deal["url"], priority=5, tags=["rotating_light"]),
            self.email(title, f"{body}\n\n{deal['url']}", _deals_table_html([deal], title)),
            self.whatsapp(f"{title}\n{body}\n{deal['url']}", "URGENT"),
        ]
        return any(results)

    def send_hot(self, deal: dict) -> bool:
        title = f"HOT {_off(deal)}: {deal['name']}"
        body = describe(deal)
        return any([self.push(title, body, url=deal["url"], priority=4, tags=["fire"]),
                    self.whatsapp(f"{title}\n{body}\n{deal['url']}", "HOT")])

    def send_hot_batch(self, deals: list[dict]) -> bool:
        title = f"{len(deals)} new HOT deals"
        lines = [f"{_off(d)}  {_rand(d['price'])}  {d['name'][:40]}{' ⚠' if d['suspicious'] else ''}"
                 for d in deals[:8]]
        if len(deals) > 8:
            lines.append(f"... and {len(deals) - 8} more (python main.py deals --tier HOT)")
        body = "\n".join(lines)
        return any([self.push(title, body, url=deals[0]["url"], priority=4, tags=["fire"]),
                    self.whatsapp(f"{title}\n{body}", "HOT")])

    def alert_new_deals(self, conn, deals: list[dict], confirm=None) -> dict:
        """Send alerts for URGENT and HOT deals we haven't alerted about at this price yet.

        confirm: optional function(url) -> {"price", "is_sold_out"} that checks the product page.
        Returns counts for logging.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        candidates = storage.not_yet_alerted(
            conn, [d for d in deals if d["tier"] in ("URGENT", "HOT") and not d["is_sold_out"]])
        urgent = [d for d in candidates if d["tier"] == "URGENT"]
        hot = [d for d in candidates if d["tier"] == "HOT"]
        sent = {"URGENT": 0, "HOT": 0, "skipped": 0}

        for deal in urgent:
            if confirm and self.cfg["confirm_urgent_on_product_page"]:
                try:
                    live = confirm(deal["url"])
                except Exception as err:
                    # Can't check? Alert anyway: a false alarm beats missing a free item.
                    print(f"  ! couldn't check product page ({err}); alerting anyway")
                    live = {"price": deal["price"], "is_sold_out": False}
                if live["is_sold_out"] or live["price"] != deal["price"]:
                    print(f"  skip URGENT {deal['name']}: product page says "
                          f"{'sold out' if live['is_sold_out'] else 'price ' + _rand(live['price'])}")
                    # Remember it, so we don't re-check the page every 5 minutes at this price.
                    if not self.dry_run:
                        storage.record_alert(conn, {**deal, "tier": "SKIPPED"}, now)
                    sent["skipped"] += 1
                    continue
            if self.send_urgent(deal):
                storage.record_alert(conn, deal, now)
                sent["URGENT"] += 1

        if len(hot) > self.cfg["hot_batch_threshold"]:
            if self.send_hot_batch(hot):
                for deal in hot:
                    storage.record_alert(conn, deal, now)
                sent["HOT"] = len(hot)
        else:
            for deal in hot:
                if self.send_hot(deal):
                    storage.record_alert(conn, deal, now)
                    sent["HOT"] += 1
        return sent

    # --- daily digest -----------------------------------------------------------------

    def send_digest(self, deals: list[dict]) -> bool:
        """Email the top deals (already ranked) as a table."""
        top = [d for d in deals if d["discount"] is not None and not d["is_sold_out"]]
        top = top[: self.cfg["email"]["digest_size"]]
        day = datetime.now(SAST).strftime("%a %d %b %Y")
        subject = f"OneDayOnly top {len(top)} deals – {day}"
        text = "\n\n".join(f"{i}. {d['name']}\n{describe(d)}\n{d['url']}" for i, d in enumerate(top, 1))
        return self.email(subject, text, _deals_table_html(top, subject))


def _deals_table_html(deals: list[dict], heading: str) -> str:
    """A simple HTML table. Inline styles, because many email apps ignore <style> blocks."""
    cell = "padding:6px 10px;border-bottom:1px solid #ddd;"
    rows = []
    for i, d in enumerate(deals, 1):
        warn = " ⚠" if d["suspicious"] else ""
        note = ", ".join(f for f in d["flags"])
        rows.append(
            f"<tr><td style='{cell}'>{i}</td>"
            f"<td style='{cell}'><b>{d['tier']}</b></td>"
            f"<td style='{cell}text-align:right'>{_off(d).replace(' off', '')}{warn}</td>"
            f"<td style='{cell}'><a href='{html.escape(d['url'])}'>{html.escape(d['name'])}</a><br>"
            f"<small>{html.escape(d['brand'] or '')} · {html.escape(d['section'])}</small></td>"
            f"<td style='{cell}text-align:right'>{_rand(d['price'])}</td>"
            f"<td style='{cell}text-align:right;color:#888'><s>{_rand(d['was_price'])}</s></td>"
            f"<td style='{cell}color:#888'><small>{html.escape(note)}</small></td></tr>")
    head = "".join(f"<th style='{cell}text-align:left'>{h}</th>"
                   for h in ("#", "Tier", "Off", "Deal", "Price", "Was", "Flags"))
    return (f"<html><body style='font-family:Arial,sans-serif'><h2>{html.escape(heading)}</h2>"
            f"<table style='border-collapse:collapse'><tr>{head}</tr>{''.join(rows)}</table>"
            f"<p style='color:#888'>⚠ = the was-price may not be trustworthy. "
            f"Prices can change; ODO may cancel orders with obvious pricing errors.</p></body></html>")
