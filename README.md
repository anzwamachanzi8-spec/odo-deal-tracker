# OneDayOnly Deal Tracker

Tracks deals on [onedayonly.co.za](https://www.onedayonly.co.za), keeps price history in SQLite,
ranks deals by discount, and pushes 100%-off deals to your phone.

- **Fast check** (every 5 min): today's deals, 1 request, URGENT/HOT alerts.
- **Full run** (every 30 min): today's deals + Extra Time + Clearance + Everyday Essentials (4 requests).
- **Daily digest** email: top 15 deals by discount.

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
- **Database size:** fast-check rows older than 2 days are deleted. Full runs (every 30 min) are
  kept forever and hold the price history.

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
Phase 7 (GitHub Actions) avoids all of this by running in the cloud.

## 4. Changing settings

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
| Digest time (SAST), failure-alert count, fast-row retention | `[schedule]` | 08:00, 3, 2 days |

Changes take effect on the next run. There's no need to reinstall the task.

## 5. How it works

| File | Job |
|---|---|
| `odo_tracker/http_client.py` | Polite requests: User-Agent, 2–3 s pauses, retries with backoff, stops if blocked |
| `odo_tracker/parser.py` | Pulls products out of the site's embedded Next.js JSON |
| `odo_tracker/fetcher.py` | Fast/full fetch, product-page check, rate limits |
| `odo_tracker/storage.py` | SQLite: append-only snapshots, change detection, alert de-duplication, pruning |
| `odo_tracker/scoring.py` | Discount %, tiers, suspicious was-price flags |
| `odo_tracker/notify.py`, `channels.py` | ntfy / email / WhatsApp alerts and the digest |
| `main.py` | Commands, the scheduled `run`, failure tracking |

Notes:
- ODO's terms (§3.1) let them cancel orders with obvious pricing errors, so a 100%-off alert is not a guarantee.
- Clearance only ever shows 600 items, and which 600 rotates. Items moving in and out of view
  show as `returned` / `removed`, not as new stock.
