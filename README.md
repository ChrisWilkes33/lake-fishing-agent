# 🎣 Lake Fishing Agent

An AI agent that monitors fishing reports for Lake Buchanan (or any lake) and sends a nightly email digest with summaries written in angler shorthand.

## How it works

**Agent 1 — Discoverer** (`discoverer.py`)
Run this once per lake. It searches for fishing guides and report sources, scrapes their sites, and saves everything to `discovered_sources.json`.

**Agent 2 — Monitor** (`monitor.py`)
Runs nightly via cron. Reads `discovered_sources.json`, visits every source, finds new reports, deduplicates them, summarizes each one, and sends one email. Tracks everything it's sent in `sent_reports.json` so you never get duplicates.

---

## Deployment on your Nanode (Linux)

### 1. Clone the repo

```bash
git clone https://github.com/ChrisWilkes33/lake-fishing-agent.git
cd lake-fishing-agent
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up your environment variables

```bash
cp .env.example .env
nano .env
```

Fill in:
- `ANTHROPIC_API_KEY` — from [console.anthropic.com](https://console.anthropic.com)
- `GMAIL_ADDRESS` — the Gmail account sending the alerts
- `GMAIL_APP_PASSWORD` — **not your Gmail password** — see below
- `ALERT_EMAIL` — where to send the reports (can be same as above)

### 4. Set up Gmail App Password

Gmail requires an App Password for SMTP access (not your regular password).

1. Go to [myaccount.google.com](https://myaccount.google.com)
2. Search for "App Passwords" in the search bar
3. Create a new App Password — name it "fishing agent"
4. Copy the 16-character password into `GMAIL_APP_PASSWORD` in your `.env`

> Note: App Passwords require 2-Step Verification to be enabled on your Google account.

### 5. Create the logs directory

```bash
mkdir -p logs
```

### 6. Run the Discoverer (one time per lake)

```bash
python discoverer.py
```

This runs the AI research agent and produces `discovered_sources.json`. Takes 2-5 minutes. Check the output — if it found fewer than 5 sources, run it again.

To run for a different lake:
```bash
python discoverer.py --lake "Lake Travis Texas"
```

### 7. Test the Monitor

```bash
python monitor.py --test
```

This runs the full monitor, sends a real email to your `ALERT_EMAIL`, and prints a detailed cost breakdown. Check your inbox and review the cost estimate before setting up cron.

### 8. Set up cron (midnight daily)

```bash
crontab -e
```

Add this line (update the path to match where you cloned the repo):

```
0 0 * * * cd /root/lake-fishing-agent && python monitor.py >> logs/monitor.log 2>&1
```

To switch to Thursdays only (once you've evaluated cost):
```
0 0 * * 4 cd /root/lake-fishing-agent && python monitor.py >> logs/monitor.log 2>&1
```

---

## File reference

| File | Purpose |
|------|---------|
| `discoverer.py` | Agent 1 — finds sources, run manually |
| `monitor.py` | Agent 2 — checks sources nightly, run by cron |
| `discovered_sources.json` | Output of discoverer, input to monitor |
| `sent_reports.json` | Deduplication tracker — never delete this |
| `logs/monitor.log` | Appended each cron run |
| `.env` | Your secrets — never commit this |

---

## Cost estimates

Both agents use Claude Haiku (cheapest model, ~10x less than Sonnet).

| Schedule | Estimated cost |
|----------|---------------|
| Daily | ~$2-5/month |
| Weekly (Thursdays) | ~$0.25-1/month |

Run `python monitor.py --test` to see the exact cost for your setup before committing to a schedule.

---

## Troubleshooting

**Email not sending**
- Make sure you're using an App Password, not your regular Gmail password
- Make sure 2-Step Verification is enabled on your Google account
- Check that `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, and `ALERT_EMAIL` are all set in `.env`

**Agent finds no reports**
- DuckDuckGo occasionally blocks scraping — wait an hour and try again
- Re-run `discoverer.py` to refresh the source list

**Cron not running**
- Check `logs/monitor.log` for errors
- Make sure the path in the cron line is correct: `cd /path/to/lake-fishing-agent`
- Test the cron command manually first by copy-pasting it into your terminal

**Too many duplicate reports**
- Don't delete `sent_reports.json` — that's what prevents duplicates
- If you want to reset and resend everything, delete `sent_reports.json` and run again
