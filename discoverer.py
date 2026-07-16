"""
discoverer.py — Agent 1: The Guide & Source Discoverer
=======================================================
Run this manually whenever you want to add a new lake or refresh the source list.
It searches for fishing guides and report sources for a given lake and saves
everything to discovered_sources.json, which the monitor reads every night.

HOW TO RUN:
    python discoverer.py
    python discoverer.py --lake "Lake Travis Texas"   # override the default lake

WHAT IT PRODUCES:
    discovered_sources.json — list of guides and report sources for the monitor to watch

COST ESTIMATE:
    ~$0.05-0.10 per run (one-time cost, not recurring)
"""

import anthropic
import requests
import json
import time
import re
import os
import argparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

DEFAULT_LAKE = "Lake Buchanan Texas"
OUTPUT_FILE = "discovered_sources.json"
REQUEST_DELAY = 1.5

# Hard cap on agent iterations — prevents runaway loops and surprise bills.
# 4 searches + ~6 scrapes = ~10 tool calls. 15 gives comfortable headroom.
MAX_ITERATIONS = 15


# ─────────────────────────────────────────────
# TOOL: scrape_page
# Fetches a webpage and returns its visible text, stripped of HTML noise.
# Truncated to 4000 chars to keep token costs down.
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

        # 4000 chars ≈ 1000 tokens — enough to identify a source without burning budget
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
# TOOL: search_web
# Scrapes DuckDuckGo HTML results — free, no API key needed.
# Returns top 5 results to keep token count low.
# ─────────────────────────────────────────────

def search_web(query: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        response = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers=headers,
            timeout=10
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        results = []

        for div in soup.find_all("div", class_="result")[:5]:
            title_tag = div.find("a", class_="result__a")
            snippet_tag = div.find("a", class_="result__snippet")
            if title_tag:
                results.append(
                    f"Title: {title_tag.get_text(strip=True)}\n"
                    f"URL: {title_tag.get('href', '')}\n"
                    f"Snippet: {snippet_tag.get_text(strip=True) if snippet_tag else 'No description'}"
                )

        time.sleep(REQUEST_DELAY)
        return "\n\n---\n\n".join(results) if results else "No results found."

    except requests.exceptions.RequestException as e:
        return f"ERROR: Search failed — {str(e)}"


# ─────────────────────────────────────────────
# TOOL REGISTRY
# Formal descriptions sent to the AI so it knows what actions are available.
# ─────────────────────────────────────────────

TOOLS = [
    {
        "name": "search_web",
        "description": (
            "Search DuckDuckGo for fishing guides or report sources for a lake. "
            "Returns titles, URLs, and descriptions of the top 5 results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "scrape_page",
        "description": (
            "Fetch and read a webpage's text content. Use after finding a promising "
            "URL to get guide details, social media handles, or report page info."
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


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Routes the AI's tool request to the correct Python function."""
    print(f"\n  🔧 {tool_name}: {json.dumps(tool_input)}")
    if tool_name == "search_web":
        result = search_web(tool_input["query"])
    elif tool_name == "scrape_page":
        result = scrape_page(tool_input["url"])
    else:
        result = f"ERROR: Unknown tool '{tool_name}'"
    print(f"  📤 {result[:120].replace(chr(10), ' ')}...")
    return result


def get_system_prompt(lake_name: str) -> str:
    """
    Builds the system prompt for a specific lake.
    Exact instructions — no vague 'be thorough' language that causes loops.
    """
    return f"""
You are a research agent finding fishing guides and report sources for {lake_name}.

Do exactly these 4 searches in order:
1. search_web("{lake_name} fishing guides")
2. search_web("{lake_name} fishing report")
3. search_web("{lake_name} bass fishing")
4. search_web("TPWD {lake_name} lake conditions")

After each search, scrape 1-2 of the most promising URLs.
Total tool calls: roughly 8-12. After that, output the JSON immediately.

OUTPUT (output this when done, nothing else):
{{
  "lake": "{lake_name}",
  "guides": [
    {{
      "name": "Guide or business name",
      "website": "https://...",
      "facebook": "URL or null",
      "instagram": "URL or null",
      "youtube": "URL or null",
      "notes": "Brief notes about this guide"
    }}
  ],
  "report_sources": [
    {{
      "name": "Source name",
      "url": "https://...",
      "type": "forum | state_agency | news | youtube | other",
      "notes": "What kind of fishing reports this source posts"
    }}
  ]
}}
"""


def save_results(final_text: str) -> bool:
    """
    Extracts JSON from the agent's output and saves to OUTPUT_FILE.
    Returns True on success, False on failure.
    """
    json_match = re.search(r'\{[\s\S]*\}', final_text)
    if json_match:
        try:
            data = json.loads(json_match.group())
            with open(OUTPUT_FILE, "w") as f:
                json.dump(data, f, indent=2)
            print(f"\n✅ Saved to {OUTPUT_FILE}")
            print(f"   Guides:         {len(data.get('guides', []))}")
            print(f"   Report sources: {len(data.get('report_sources', []))}")
            return True
        except json.JSONDecodeError as e:
            print(f"\n❌ JSON parse error: {e}")

    print("Saving raw output to raw_output.txt")
    with open("raw_output.txt", "w") as f:
        f.write(final_text)
    return False


# ─────────────────────────────────────────────
# THE AGENT LOOP
# ─────────────────────────────────────────────

def run_discoverer(lake_name: str):
    client = anthropic.Anthropic()
    system_prompt = get_system_prompt(lake_name)

    messages = [{
        "role": "user",
        "content": f"Find fishing guides and report sources for {lake_name}. Follow your instructions, then output the JSON."
    }]

    print(f"\n{'='*60}")
    print(f"  🎣 Discoverer — {lake_name}")
    print(f"  Max iterations: {MAX_ITERATIONS}")
    print(f"{'='*60}\n")

    total_input_tokens = 0
    total_output_tokens = 0
    iteration = 0

    while True:
        iteration += 1

        # ── HARD STOP: force a summary if we hit the cap ──
        if iteration > MAX_ITERATIONS:
            print(f"\n⚠️  Hit iteration cap. Forcing summary...")
            messages.append({
                "role": "user",
                "content": "STOP. No more tool calls. Output the JSON summary now based on what you have found."
            })
            final_response = client.messages.create(
                model="claude-haiku-4-5-20251001",  # cheaper model for forced summary
                max_tokens=2048,
                system=system_prompt,
                tools=[],           # no tools — AI cannot loop further
                messages=messages
            )
            final_text = "".join(b.text for b in final_response.content if hasattr(b, "text"))
            save_results(final_text)
            break

        print(f"\n--- Iteration {iteration}/{MAX_ITERATIONS} ---")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",  # Haiku is ~10x cheaper than Sonnet, fine for research
            max_tokens=1024,
            system=system_prompt,
            tools=TOOLS,
            messages=messages
        )

        # Track and display token usage after every call
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens
        cost = (total_input_tokens / 1_000_000 * 0.80) + (total_output_tokens / 1_000_000 * 4.00)
        print(f"  Tokens: {response.usage.input_tokens} in / {response.usage.output_tokens} out | Running cost: ~${cost:.4f}")
        print(f"  Stop reason: {response.stop_reason}")

        if response.stop_reason == "end_turn":
            print("\n✅ Agent finished.")
            final_text = "".join(b.text for b in response.content if hasattr(b, "text"))
            save_results(final_text)
            break

        elif response.stop_reason == "tool_use":
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in tool_use_blocks:
                result_text = execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text
                })
            messages.append({"role": "user", "content": tool_results})

        else:
            print(f"\n⚠️  Unexpected stop reason: {response.stop_reason}. Exiting.")
            break

    cost = (total_input_tokens / 1_000_000 * 0.80) + (total_output_tokens / 1_000_000 * 4.00)
    print(f"\n💰 Total: {total_input_tokens} input / {total_output_tokens} output tokens — ~${cost:.4f}\n")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY not set. Check your .env file.")
        exit(1)

    parser = argparse.ArgumentParser(description="Discover fishing sources for a lake")
    parser.add_argument("--lake", default=DEFAULT_LAKE, help="Lake name to research")
    args = parser.parse_args()

    run_discoverer(args.lake)
