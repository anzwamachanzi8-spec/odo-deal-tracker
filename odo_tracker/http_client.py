"""A small, polite HTTP client: custom User-Agent, pauses, retries with backoff.

If the site looks like it is blocking us (403 or a Cloudflare challenge page), we raise
BlockedError and stop, instead of trying to get around it.
"""

import random
import time

import requests


class BlockedError(Exception):
    """The site refused us. Stop and tell the user; don't work around it."""


# Status codes worth retrying: rate limited (429) or a temporary server problem (5xx).
RETRY_STATUSES = {429, 500, 502, 503, 504}


class PoliteClient:
    def __init__(self, politeness: dict):
        self.p = politeness
        self.session = requests.Session()  # reuses the connection between requests
        self.session.headers.update({
            "User-Agent": self.p["user_agent"],
            "Accept-Encoding": "gzip, deflate",  # compressed responses are ~10x smaller
        })
        self._last_request_at = None
        self.request_count = 0

    def _pause_between_requests(self):
        """Wait 2-3 seconds (from config) since the previous request, if there was one."""
        if self._last_request_at is None:
            return
        wanted = random.uniform(self.p["delay_min_seconds"], self.p["delay_max_seconds"])
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < wanted:
            time.sleep(wanted - elapsed)

    def get(self, url: str) -> requests.Response:
        """GET a URL politely. Returns the response (including 404s, which the caller handles)."""
        attempts = 1 + self.p["max_retries"]
        for attempt in range(1, attempts + 1):
            self._pause_between_requests()
            self._last_request_at = time.monotonic()
            self.request_count += 1
            try:
                resp = self.session.get(url, timeout=self.p["timeout_seconds"])
            except requests.RequestException as err:
                # Network problem (dropped connection, timeout): retry.
                problem = f"{type(err).__name__}: {err}"
                resp = None
            else:
                if resp.status_code == 403 or _looks_like_challenge(resp):
                    raise BlockedError(f"{url} returned {resp.status_code}; the site may be blocking automated access.")
                if resp.status_code not in RETRY_STATUSES:
                    return resp
                problem = f"HTTP {resp.status_code}"

            if attempt == attempts:
                raise RuntimeError(f"Giving up on {url} after {attempts} attempts ({problem})")

            # Exponential backoff: 5s, 15s, 45s... Honour Retry-After if the server sends one.
            wait = self.p["backoff_base_seconds"] * (3 ** (attempt - 1))
            if resp is not None and resp.headers.get("Retry-After", "").isdigit():
                wait = max(wait, int(resp.headers["Retry-After"]))
            print(f"  ! {problem} on {url}; retry {attempt}/{attempts - 1} in {wait}s")
            time.sleep(wait)


def _looks_like_challenge(resp: requests.Response) -> bool:
    """Cloudflare's 'checking your browser' page, served instead of real content."""
    if "text/html" not in resp.headers.get("Content-Type", ""):
        return False
    head = resp.text[:5000]
    return "cf-chl" in head or "Just a moment..." in head
