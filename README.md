# OneDayOnly Deal Tracker

Tracks deals on [onedayonly.co.za](https://www.onedayonly.co.za), keeps price history in SQLite,
ranks deals by discount, and pushes 100%-off deals to your phone.

- **Fast check** (every 5 min): today's deals, 1 request, URGENT/HOT alerts.
- **Full run** (every 30 min): today's deals + Extra Time + Clearance + Everyday Essentials (4 requests).
- **Daily digest** email: top 15 deals by discount.
- **Daily heartbeat** push at 08:00: "still running", so a quiet phone isn't ambiguous.

It runs either on your laptop (Windows Task Scheduler, section 3) or on GitHub's servers
(GitHub Actions, section 4). Use one or the other, never both: running both doubles the
requests to the site.

Site findings (robots.txt, terms, how the data loads) are in [docs/phase1-findings.md](docs/phase1-findings.md).

---

## 1. Setup (Windows, once)

```powershell
cd odo-deal-tracker
py -3.12 -m venv .venv                 # any Python 3.11+ works
.\.venv\Scripts\Activate.ps1           # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
copy .env.example .env
```

Then fill in `.env` (never commit it; it's in `.gitignore`):

| Variable | What | How to get it |
|---|---|---|
| `NTFY_TOPIC` | Your private push topic | Make up a long random name, e.g. `odo-deals-k3j9x2q8`. Install the **ntfy** app and subscribe to it. |
| `SMTP_USER` | Your Gmail address | |
| `SMTP_PASSWORD` | A Gmail **app password** (not your normal password) | Google Account → Security → 2-Step Verification (must be on) → App passwords → create one. |
| `TWILIO_*` | Only for optional WhatsApp | twilio.com free trial → WhatsApp sandbox |

And set your email address in `config.toml` under `[notifications.email]` → `to = ["you@gmail.com"]`.

Test every channel with a fake deal:

```powershell
python main.py test-notify --dry-run   # preview only
python main.py test-notify             # really send
```

## 2. Running by hand

```powershell
python main.py fetch --mode full       # fetch everything, save, show changes, send alerts
python main.py fetch --mode fast       # today's deals only
python main.py deals --top 20          # best deals from the last full run (no site requests)
python main.py deals --tier HOT
python main.py deals --suspicious      # deals whose was-price looks untrustworthy
python main.py digest --force          # send the digest email now
python -m unittest discover -s tests -t .   # run the tests
```

Runs that come too soon are skipped (minimum 5 min between homepage requests, 30 min between
full runs). That's on purpose, to be polite to the site.

## 3. Running automatically (Windows Task Scheduler)

One task runs `main.py run` every 5 minutes. Each tick does a **full run** if 30 minutes have
passed, otherwise a **fast check**. Full runs include today's deals, so the URGENT check really
happens every 5 minutes, and the two jobs can never collide.

Install (no admin needed):

```powershell
powershell -ExecutionPolicy Bypass -File scheduler\install_task.ps1
```

Remove: `powershell -ExecutionPolicy Bypass -File scheduler\uninstall_task.ps1`

- **Logs:** `logs\tracker.log`, timestamped. It rotates at 1 MB and keeps 5 old files.
  Watch it live: `Get-Content logs\tracker.log -Wait -Tail 20`
- **Failure alert:** after 3 failed runs in a row you get a push, and another when it recovers.
- **Blocked by the site** (403 or a Cloudflare challenge): the tracker **pauses itself**, pushes
  an alert and stops making requests. Check the site in a browser, then `python main.py resume`.
- **Task status:** open `taskschd.msc` → Task Scheduler Library → "ODO Deal Tracker".
  "Last Run Result" 0x0 = OK, 0x1 = failed run, 0x2 = paused (blocked).
- **Database size:** a product is only saved again when something about it changes, so the
  database grows by well under 1 MB a day. (The first version saved every product on every run:
  19 MB in one day. `python scripts/migrate_db.py` converts a database from that format.)

### ⚠ The tracker stops when your laptop sleeps

Task Scheduler only runs while Windows is awake **and you're logged in**. Sleep, hibernate,
closing the lid, shutting down or signing out all stop it. (A locked screen is fine.) After waking
it runs once to catch up, but deals that came and went while it slept are missed, and a sleeping
laptop can't send a failure alert either.

To keep it running, keep the laptop **plugged in** and stop it sleeping on mains power.

**Through Settings:**
1. Settings → System → Power & battery → Screen and sleep →
   "When plugged in, put my device to sleep after" → **Never**.
   (The screen may still turn off; that's fine.)
2. Control Panel → Hardware and Sound → Power Options → "Choose what closing the lid does" →
   "When I close the lid" → Plugged in: **Do nothing** → Save changes.

**Or the same in PowerShell** (affects plugged-in only; battery settings are untouched):

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
```

To undo, set sleep back in Settings (e.g. 30 minutes) and the lid action back to "Sleep".

Also: Windows Update restarts sign you out, so the task won't run again until you log back in.
GitHub Actions (next section) avoids all of this by running in the cloud.

## 4. Running in the cloud (GitHub Actions)

[.github/workflows/tracker.yml](.github/workflows/tracker.yml) runs `main.py run` on GitHub's
servers: a `*/5` schedule for the URGENT check, a `*/30` schedule for the full run, and a
"Run workflow" button. The code repo is public (public repos get unlimited free Actions minutes;
private ones only 2,000 a month).

**Where data lives.** ODO's terms (§18.1) don't allow republishing their content, so the
database is **not** in the public repo. It's in a separate private repo,
`odo-deal-tracker-data`. Each run downloads it, and pushes it back **only if the run found
something new** (a product change or an alert). The data repo is kept at a single commit,
replaced each time, so it doesn't fill up with old copies of the database. Small things that
change every run (run times, failure count) go in GitHub's cache instead of a commit.

**Secrets** (repo → Settings → Secrets and variables → Actions → New repository secret):

| Secret | What |
|---|---|
| `DATA_REPO_TOKEN` | Fine-grained token: github.com → Settings → Developer settings → Fine-grained tokens. Repository access: **only** `odo-deal-tracker-data`. Permissions: **Contents: Read and write**. |
| `NTFY_TOPIC` | Same as in `.env` |
| `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` | Optional: email. `EMAIL_TO` keeps your address out of the public `config.toml`. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `WHATSAPP_TO` | Optional: WhatsApp |

Set them from PowerShell without the value ending up anywhere else (it asks you to paste it):
`gh secret set NTFY_TOPIC`

**Manual actions:** Actions tab → Tracker → Run workflow → pick `run`, `test-notify`
(send fake alerts), `resume` (after the site blocked the tracker and it paused itself) or
`keepalive`. Or from PowerShell: `gh workflow run tracker.yml -f action=test-notify`

**What to expect:**
- **Alert delay of about 5–15 minutes.** GitHub's schedule is best-effort: runs often start
  late, and some are dropped when GitHub is busy, especially around the start of each hour.
  Two runs that arrive less than 5 minutes apart: the second skips (rate limit).
- **Heartbeat** push every morning after 08:00. No heartbeat = look at the Actions tab.
- **Failure alert** after 3 failed runs in a row, from the tracker or from a broken workflow
  step. GitHub also emails you when a run fails.
- **The 60-day rule:** GitHub disables scheduled workflows in a public repo after 60 days
  without "repository activity". Its docs don't define activity, and the database commits go to
  the private repo anyway. So a weekly job commits a dated file if nobody has committed for
  40 days, and calls GitHub's "enable workflow" API. If the heartbeat still stops, re-enable:
  `gh workflow enable tracker.yml` (or the "Enable workflow" button on the Actions tab).

**Logs:** Actions tab → a run → "Run the tracker". Each run's log is kept for 90 days.

**Switching between laptop and cloud.** Only one may run at a time.
- Laptop → cloud: `powershell -ExecutionPolicy Bypass -File scheduler\uninstall_task.ps1`, then
  push your latest database to the data repo (copy `data\odo_deals.db` and `data\run_state.json`
  into a clone of `odo-deal-tracker-data`, commit, push), then `gh workflow enable tracker.yml`.
- Cloud → laptop: `gh workflow disable tracker.yml`, download the data repo's files into
  `data\`, then install the task again.

## 5. Changing settings

Everything is in **`config.toml`**. Secrets stay in `.env`.

| Setting | Section | Default |
|---|---|---|
| URGENT / HOT thresholds | `[scoring]` `urgent_discount`, `hot_discount` | 1.0 / 0.70 |
| Suspicious was-price sensitivity | `[scoring]` `was_price_jump_factor`, `was_price_vs_seen_price_factor` | 1.25 / 5.0 |
| Pages in the full run | `[site]` `full_run_pages` | Extra Time, Clearance, Everyday Essentials |
| Delays, retries, minimum gaps | `[politeness]` | 2–3 s, 3 retries, 5 / 30 min |
| Email recipients, digest size | `[notifications.email]` | 15 deals |
| Batch HOT pushes above | `[notifications]` `hot_batch_threshold` | 3 |
| Check product page before URGENT | `[notifications]` `confirm_urgent_on_product_page` | true |
| WhatsApp on/off | `[notifications.whatsapp]` `enabled` | false |
| Digest time (SAST), failure-alert count | `[schedule]` | 08:00, 3 |
| Daily heartbeat on/off, time (SAST) | `[schedule]` `heartbeat`, `heartbeat_time` | on, 08:00 |

Changes take effect on the next run. There's no need to reinstall the task. In the cloud,
commit and push the changed `config.toml`.

## 6. How it works

| File | Job |
|---|---|
| `odo_tracker/http_client.py` | Polite requests: User-Agent, 2–3 s pauses, retries with backoff, stops if blocked |
| `odo_tracker/parser.py` | Pulls products out of the site's embedded Next.js JSON |
| `odo_tracker/fetcher.py` | Fast/full fetch, product-page check, rate limits |
| `odo_tracker/storage.py` | SQLite: product versions saved only on change, presence streaks, change detection, alert de-duplication |
| `odo_tracker/scoring.py` | Discount %, tiers, suspicious was-price flags |
| `odo_tracker/notify.py`, `channels.py` | ntfy / email / WhatsApp alerts and the digest |
| `main.py` | Commands, the scheduled `run`, failure tracking, heartbeat |
| `.github/workflows/tracker.yml` | The cloud schedule, data repo download/upload, keepalive |
| `scripts/migrate_db.py` | Converts an old-format database |
| `scripts/infra_failure.py` | Counts cloud failures that happen before the tracker starts |
| `scripts/reachability_test.py` | The one-off "can GitHub reach the site?" test (2 requests) |

Notes:
- ODO's terms (§3.1) let them cancel orders with obvious pricing errors, so a 100%-off alert is not a guarantee.
- Clearance only ever shows 600 items, and which 600 rotates. Items moving in and out of view
  show as `returned` / `removed`, not as new stock.
