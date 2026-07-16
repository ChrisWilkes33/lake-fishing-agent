"""
monitor.py — Agent 2: The Nightly Fishing Report Monitor
=========================================================
Reads discovered_sources.json, visits every source, finds new fishing reports,
summarizes them in angler shorthand, and sends one email digest.

HOW TO RUN:
    python monitor.py            # normal run (also how cron runs it)
    python monitor.py --test     # same thing — sends real email, writes real state,
                                 # but prints detailed cost breakdown at the end

CRON SETUP (midnight daily):
    0 0 * * * cd /path/to/lake-fishing-agent && python monitor.py >> logs/monitor.log 2>&1

WHAT IT PRODUCES:
    sent_reports.json — tracks every report ever sent so nothing is duplicated
    logs/monitor.log  — appended each run (when triggered by cron)

COST ESTIMATE:
    ~$0.05-0.15 per run depending on how many sources were found by the discoverer
"""

import anthropic
import requests
import json
import time
import re
import os
import hashlib
import argparse
import smtplib

from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

SOURCES_FILE = "discovered_sources.json"   # Written by discoverer.py
SENT_REPORTS_FILE = "sent_reports.json"    # Our deduplication tracker

REQUEST_DELAY = 1.5
MAX_ITERATIONS = 20   # Hard cap on the scraping loop

# How old a dated report can be before we ignore it.
# 7 days catches weekly reports and guides who post every few days.
MAX_REPORT_AGE_DAYS = 7


# ─────────────────────────────────────────────
# STATE: sent reports tracker
#
# sent_reports.json looks like this:
# {
#   "abc123...": {                    <- MD5 hash of report content
#     "sent_at": "2024-01-15 06:00",
#     "source": "Lake Buchanan Fishing Guide",
#     "summary": "Bass holding at 18ft near dam..."
#   }
# }
#
# We use the hash as the key so we never send the same report twice,
# even if the source changes its formatting.
# ─────────────────────────────────────────────

def load_sent_reports() -> dict:
    """Loads the sent reports tracker from disk. Returns empty dict if file doesn't exist yet."""
    if os.path.exists(SENT_REPORTS_FILE):
        with open(SENT_REPORTS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_sent_reports(sent_reports: dict):
    """Saves the updated sent reports tracker back to disk."""
    with open(SENT_REPORTS_FILE, "w") as f:
        json.dump(sent_reports, f, indent=2)


def fingerprint(text: str) -> str:
    """
    Creates an MD5 hash of a report's text content.
    This is our deduplication key — same content = same hash = already sent.
    We strip whitespace first so minor formatting changes don't create false 'new' reports.
    """
    normalized = re.sub(r'\s+', ' ', text.strip().lower())
    return hashlib.md5(normalized.encode()).hexdigest()


# ─────────────────────────────────────────────
# DATE DETECTION
#
# Fishing reports use wildly inconsistent date formats.
# We try a few common patterns and return None if we can't find one.
# ─────────────────────────────────────────────

DATE_PATTERNS = [
    r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b',           # 1/15/2024 or 01-15-24
    r'\b(January|February|March|April|May|June|July|'
    r'August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b',  # January 15, 2024
    r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(\d{4})\b',  # Jan 15, 2024
    r'\b(\d{4})-(\d{2})-(\d{2})\b',                        # 2024-01-15 (ISO format)
]

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
}


def extract_date(text: str) -> datetime | None:
    """
    Tries to extract the most recent date mentioned in a block of text.
    Returns a datetime object or None if no date was found.
    """
    found_dates = []

    for pattern in DATE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            groups = match.groups()
            try:
                # ISO format: 2024-01-15
                if len(groups) == 3 and len(groups[0]) == 4:
                    dt = datetime(int(groups[0]), int(groups[1]), int(groups[2]))
                # Month name: January 15, 2024
                elif groups[0].lower() in MONTH_MAP:
                    month = MONTH_MAP[groups[0].lower()]
                    dt = datetime(int(groups[2]), month, int(groups[1]))
                # Numeric: 1/15/2024
                else:
                    year = int(groups[2])
                    if year < 100:
                        year += 2000
                    dt = datetime(year, int(groups[0]), int(groups[1]))

                # Sanity check — ignore dates more than a year in the past or future
                now = datetime.now()
                if abs((dt - now).days) < 365:
                    found_dates.append(dt)
            except (ValueError, IndexError):
                continue

    # Return the most recent date found (most likely to be the report date)
    return max(found_dates) if found_dates else None


def is_recent(text: str) -> tuple[bool, str]:
    """
    Determines if a report is recent enough to send.

    Returns:
        (True, "dated: 2024-01-15") if dated and within MAX_REPORT_AGE_DAYS
        (True, "undated: new fingerprint") if no date but not seen before
        (False, reason) if it should be skipped
    """
    date = extract_date(text)
    if date:
        age_days = (datetime.now() - date).days
        if age_days <= MAX_REPORT_AGE_DAYS:
            return True, f"dated: {date.strftime('%Y-%m-%d')} ({age_days} days ago)"
        else:
            return False, f"dated but too old: {date.strftime('%Y-%m-%d')} ({age_days} days ago)"
    else:
        # No date found — we'll let the fingerprint check handle deduplication
        return True, "undated: will check fingerprint"


# ─────────────────────────────────────────────
# TOOL: scrape_page
# Same as in discoverer.py — fetch and return visible page text.
# ─────────────────────────────────────────────

def scrape_page(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()

        text = soup.get_text(separator="\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        if len(text) > 4000:
            text = text[:4000] + "\n\n[...truncated...]"

        time.sleep(REQUEST_DELAY)
        return text

    except requests.exceptions.Timeout:
        return f"ERROR: Timed out fetching {url}"
    except requests.exceptions.HTTPError as e:
        return f"ERROR: HTTP {e.response.status_code} for {url}"
    except requests.exceptions.RequestException as e:
        return f"ERROR: Could not fetch {url} — {str(e)}"


# ─────────────────────────────────────────────
# SUMMARIZATION
#
# This is a separate, bounded API call — not part of the agent loop.
# One call per new report. Small, predictable cost.
# ─────────────────────────────────────────────

def summarize_report(client: anthropic.Anthropic, report_text: str, source_name: str) -> str:
    """
    Asks the AI to summarize a fishing report in angler shorthand.
    Focuses on: depth, structure/location on lake, what's biting, conditions.
    Returns a 3-5 sentence summary.
    """
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",  # Cheapest model — summarization doesn't need Sonnet
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                f"Summarize this fishing report from '{source_name}' in 3-5 sentences "
                f"for an experienced angler. Focus on: what depth fish are holding at, "
                f"what part of the lake or what structure, what species and what they're biting on, "
                f"and overall conditions. Use angler shorthand — creek channels, main lake points, "
                f"staging areas, etc. Be concise, no fluff.\n\n"
                f"Report:\n{report_text[:3000]}"
            )
        }]
    )
    return response.content[0].text.strip()


# ─────────────────────────────────────────────
# THE MONITOR AGENT LOOP
#
# The agent's job: visit each source URL, find any fishing reports on the page,
# and return them as structured data. We then handle deduplication and
# summarization outside the loop (cheaper and more predictable).
# ─────────────────────────────────────────────

MONITOR_TOOLS = [
    {
        "name": "scrape_page",
        "description": (
            "Fetch and read a webpage. Use this to check a fishing source for recent reports. "
            "Returns the visible text of the page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL including https://"}
            },
            "required": ["url"]
        }
    }
]


def run_monitor_agent(client: anthropic.Anthropic, sources: list) -> list:
    """
    Runs the monitor agent loop over all sources.
    Returns a list of raw report dicts found:
    [{"source": "name", "url": "...", "content": "raw text of the report"}]
    """

    # Build a formatted list of sources for the system prompt
    source_list = "\n".join(
        f"- {s['name']}: {s['url']}" for s in sources
    )

    system_prompt = f"""
You are a fishing report monitor. You have a list of sources to check for recent fishing reports.

SOURCES TO CHECK:
{source_list}

YOUR TASK:
Visit each URL using scrape_page. Look for any fishing reports — guide reports, catch reports,
lake condition updates, or fishing summaries. Skip sources that return errors.

After visiting all sources, output a JSON array of reports you found.
Only include actual fishing reports — not general website content, ads, or navigation text.

OUTPUT FORMAT (output this when done, nothing else):
[
  {{
    "source": "Source name",
    "url": "URL you scraped",
    "content": "The raw text of the fishing report you found (copy the relevant section)"
  }}
]

If you found no reports at all, output an empty array: []
"""

    messages = [{
        "role": "user",
        "content": "Check all the sources for recent fishing reports and output the JSON array."
    }]

    total_input_tokens = 0
    total_output_tokens = 0
    iteration = 0

    while True:
        iteration += 1

        if iteration > MAX_ITERATIONS:
            print(f"\n⚠️  Hit iteration cap. Forcing summary...")
            messages.append({
                "role": "user",
                "content": "STOP. Output the JSON array now with whatever you have found."
            })
            final_response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                system=system_prompt,
                tools=[],
                messages=messages
            )
            final_text = "".join(b.text for b in final_response.content if hasattr(b, "text"))
            return parse_reports_json(final_text), total_input_tokens, total_output_tokens

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,   # raised — the JSON summary of 16 sources needs room
            system=system_prompt,
            tools=MONITOR_TOOLS,
            messages=messages
        )

        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        print(f"  Iteration {iteration}: {response.usage.input_tokens} in / {response.usage.output_tokens} out | stop: {response.stop_reason}")

        if response.stop_reason == "end_turn":
            final_text = "".join(b.text for b in response.content if hasattr(b, "text"))
            return parse_reports_json(final_text), total_input_tokens, total_output_tokens

        # max_tokens means the AI ran out of space writing the summary.
        # Append what it wrote, then force a clean summary with no tools.
        elif response.stop_reason == "max_tokens":
            print("\n⚠️  Hit max_tokens — forcing clean summary...")
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": "STOP. No more tool calls. Output the JSON array now with what you have found."
            })
            final_response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                system=system_prompt,
                tools=[],
                messages=messages
            )
            final_text = "".join(b.text for b in final_response.content if hasattr(b, "text"))
            return parse_reports_json(final_text), total_input_tokens, total_output_tokens

        elif response.stop_reason == "tool_use":
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in tool_use_blocks:
                print(f"\n  🔧 scrape_page: {block.input.get('url', '')[:80]}")
                result = scrape_page(block.input["url"])
                print(f"  📤 {result[:100].replace(chr(10), ' ')}...")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })
            messages.append({"role": "user", "content": tool_results})

        else:
            print(f"⚠️  Unexpected stop reason: {response.stop_reason}")
            break

    return [], total_input_tokens, total_output_tokens


def parse_reports_json(text: str) -> list:
    """Extracts the JSON array of reports from the agent's final output."""
    # Look for a JSON array [...] in the output
    match = re.search(r'\[[\s\S]*\]', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return []


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────

def send_email(subject: str, body: str):
    """
    Sends an email via Gmail SMTP using App Password authentication.
    Requires GMAIL_ADDRESS, GMAIL_APP_PASSWORD, and ALERT_EMAIL in .env
    """
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]
    alert_email = os.environ["ALERT_EMAIL"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = alert_email
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_password)
        server.sendmail(gmail_address, alert_email, msg.as_string())

    print(f"📧 Email sent to {alert_email}")


def build_email(new_reports: list, lake_name: str) -> tuple[str, str]:
    """
    Builds the email subject and body.
    new_reports is a list of dicts with keys: source, url, summary, date_info
    """
    date_str = datetime.now().strftime("%B %d, %Y")

    if not new_reports:
        subject = f"🎣 {lake_name} Fishing Report — {date_str} — Nothing New"
        body = (
            f"{lake_name} Fishing Report — {date_str}\n"
            f"{'─' * 50}\n\n"
            f"No new fishing reports to share at this time.\n\n"
            f"The agent checked all known sources and found nothing posted "
            f"in the last {MAX_REPORT_AGE_DAYS} days that hasn't already been sent.\n"
        )
    else:
        subject = f"🎣 {lake_name} — {len(new_reports)} New Fishing Report{'s' if len(new_reports) > 1 else ''} — {date_str}"
        body = (
            f"{lake_name} Fishing Reports — {date_str}\n"
            f"{'─' * 50}\n\n"
        )
        for i, report in enumerate(new_reports, 1):
            body += (
                f"Report {i} of {len(new_reports)}: {report['source']}\n"
                f"Source: {report['url']}\n"
                f"Date info: {report['date_info']}\n\n"
                f"{report['summary']}\n\n"
                f"{'─' * 50}\n\n"
            )

    return subject, body


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run_monitor(test_mode: bool = False):
    """
    Full monitor run:
    1. Load sources from discoverer output
    2. Run agent to scrape all sources
    3. Deduplicate using date + fingerprint
    4. Summarize new reports
    5. Send email
    6. Update sent_reports.json
    """

    print(f"\n{'='*60}")
    print(f"  🎣 Fishing Report Monitor")
    print(f"  {'TEST MODE — ' if test_mode else ''}Run at: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    # ── Load sources ──
    if not os.path.exists(SOURCES_FILE):
        print(f"❌ {SOURCES_FILE} not found. Run discoverer.py first.")
        exit(1)

    with open(SOURCES_FILE, "r") as f:
        sources_data = json.load(f)

    lake_name = sources_data.get("lake", "Unknown Lake")

    # Combine guides and report sources into one flat list of URLs to check
    all_sources = []
    for guide in sources_data.get("guides", []):
        if guide.get("website"):
            all_sources.append({"name": guide["name"], "url": guide["website"]})
    for source in sources_data.get("report_sources", []):
        if source.get("url"):
            all_sources.append({"name": source["name"], "url": source["url"]})

    print(f"  Lake: {lake_name}")
    print(f"  Sources to check: {len(all_sources)}")

    if not all_sources:
        print("❌ No sources found in discovered_sources.json. Re-run discoverer.py.")
        exit(1)

    # ── Load sent reports tracker ──
    sent_reports = load_sent_reports()
    print(f"  Previously sent reports: {len(sent_reports)}\n")

    # ── Run the monitor agent ──
    client = anthropic.Anthropic()
    print("Running monitor agent...\n")
    raw_reports, monitor_input_tokens, monitor_output_tokens = run_monitor_agent(client, all_sources)
    print(f"\n  Agent found {len(raw_reports)} potential reports\n")

    # ── Deduplicate and summarize ──
    new_reports = []
    summarization_input_tokens = 0
    summarization_output_tokens = 0

    for raw in raw_reports:
        content = raw.get("content", "")
        if not content or len(content) < 50:
            continue  # Skip empty or near-empty results

        # Check recency (date-based)
        recent, date_info = is_recent(content)
        if not recent:
            print(f"  ⏭️  Skipping '{raw['source']}' — {date_info}")
            continue

        # Check fingerprint (deduplication)
        fp = fingerprint(content)
        if fp in sent_reports:
            print(f"  ⏭️  Skipping '{raw['source']}' — already sent on {sent_reports[fp]['sent_at']}")
            continue

        # New report — summarize it
        print(f"  ✨ New report from '{raw['source']}' ({date_info}) — summarizing...")
        summary_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": (
                    f"Summarize this fishing report from '{raw['source']}' in 3-5 sentences "
                    f"for an experienced angler. Focus on: depth fish are holding, "
                    f"location on the lake or structure type, species and what they're biting, "
                    f"and overall conditions. Use angler shorthand. Be concise.\n\n"
                    f"Report:\n{content[:3000]}"
                )
            }]
        )
        summarization_input_tokens += summary_response.usage.input_tokens
        summarization_output_tokens += summary_response.usage.output_tokens
        summary = summary_response.content[0].text.strip()

        new_reports.append({
            "source": raw["source"],
            "url": raw["url"],
            "summary": summary,
            "date_info": date_info,
            "fingerprint": fp
        })

    # ── Build and send email ──
    subject, body = build_email(new_reports, lake_name)
    print(f"\n📧 Sending email: {subject}")
    print(f"\n{body}")

    try:
        send_email(subject, body)
    except KeyError as e:
        print(f"❌ Missing email config in .env: {e}")
        print("Email not sent — check GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ALERT_EMAIL in .env")
    except Exception as e:
        print(f"❌ Email failed: {e}")

    # ── Update sent reports tracker ──
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    for report in new_reports:
        sent_reports[report["fingerprint"]] = {
            "sent_at": now_str,
            "source": report["source"],
            "summary": report["summary"]
        }
    save_sent_reports(sent_reports)
    print(f"\n💾 Updated {SENT_REPORTS_FILE} ({len(sent_reports)} total reports tracked)")

    # ── Cost breakdown (always shown in test mode, summarized otherwise) ──
    total_input = monitor_input_tokens + summarization_input_tokens
    total_output = monitor_output_tokens + summarization_output_tokens
    # Haiku pricing: $0.80/M input, $4.00/M output
    cost = (total_input / 1_000_000 * 0.80) + (total_output / 1_000_000 * 4.00)

    if test_mode:
        print(f"\n{'='*60}")
        print(f"  💰 COST BREAKDOWN (test mode)")
        print(f"{'='*60}")
        print(f"  Monitor agent:   {monitor_input_tokens:>6} in / {monitor_output_tokens:>5} out tokens")
        print(f"  Summarization:   {summarization_input_tokens:>6} in / {summarization_output_tokens:>5} out tokens")
        print(f"  Total:           {total_input:>6} in / {total_output:>5} out tokens")
        print(f"  Estimated cost:  ~${cost:.4f} per run")
        print(f"  Daily (30 days): ~${cost * 30:.2f}/month")
        print(f"  Weekly (Thurs):  ~${cost * 4:.2f}/month")
        print(f"{'='*60}\n")
    else:
        print(f"\n💰 Run cost: ~${cost:.4f}")

    print("\n🎣 Monitor complete.\n")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    required_vars = ["ANTHROPIC_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "ALERT_EMAIL"]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        print("   Copy .env.example to .env and fill in your values.")
        exit(1)

    parser = argparse.ArgumentParser(description="Monitor fishing reports and send email digest")
    parser.add_argument("--test", action="store_true", help="Run manually with detailed cost output")
    args = parser.parse_args()

    run_monitor(test_mode=args.test)
