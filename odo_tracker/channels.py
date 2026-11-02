"""Low-level senders: one function per channel. They know nothing about deals.

Each sender raises NotConfigured if its secrets are missing from .env, so the caller can
print the message to the console instead of crashing.
"""

import os
import smtplib
from email.message import EmailMessage

import requests


class NotConfigured(Exception):
    """A channel's secrets are missing from .env."""


def _secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise NotConfigured(f"{name} is not set (in .env, or as a GitHub secret in the cloud)")
    return value


def send_ntfy(cfg: dict, title: str, message: str, url: str | None = None,
              priority: int = 3, tags: list[str] | None = None):
    """Phone push via ntfy.sh. priority: 1 (min) .. 5 (urgent: bypasses Do Not Disturb on Android).

    We publish as JSON (rather than HTTP headers) so product names with characters like ’ or é
    don't break the request.
    """
    payload = {"topic": _secret("NTFY_TOPIC"), "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if url:
        payload["click"] = url  # tapping the notification opens the deal
    resp = requests.post(cfg["server"], json=payload, timeout=15)
    resp.raise_for_status()


def send_email(cfg: dict, subject: str, text: str, html: str | None = None):
    """Send an email through SMTP (Gmail by default).

    Recipients: the EMAIL_TO secret (comma-separated) if set, else config [notifications.email].to.
    The secret exists so your address doesn't have to sit in config.toml in a public repo.
    """
    user, password = _secret("SMTP_USER"), _secret("SMTP_PASSWORD")
    to = os.environ.get("EMAIL_TO", "").strip() or ", ".join(cfg["to"])
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(text)                       # plain-text version for simple mail apps
    if html:
        msg.add_alternative(html, subtype="html")  # nicer version with a table
    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as smtp:
        smtp.starttls()                         # encrypt the connection before logging in
        smtp.login(user, password)
        smtp.send_message(msg)


def send_whatsapp(cfg: dict, text: str):
    """WhatsApp message via Twilio's REST API (plain requests, no Twilio package needed).

    Your number comes from the WHATSAPP_TO secret if set, else config [notifications.whatsapp].to_number.
    """
    sid, token = _secret("TWILIO_ACCOUNT_SID"), _secret("TWILIO_AUTH_TOKEN")
    to = os.environ.get("WHATSAPP_TO", "").strip() or cfg["to_number"]
    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data={"From": cfg["from_number"], "To": to, "Body": text},
        auth=(sid, token), timeout=15,
    )
    resp.raise_for_status()
