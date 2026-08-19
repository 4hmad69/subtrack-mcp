# SubTrack — Subscription & Recurring Bill Tracker (MCP Server)

**The problem it solves:** almost everyone is quietly bleeding money from
subscriptions and recurring bills they forgot about — a streaming trial that
converted to paid, a gym membership, a yearly domain renewal that hits once
and gets forgotten for 11 months. SubTrack lets an LLM (Claude, or any MCP
client) track all of it, tell you what's renewing soon, and show you total
spend normalized to a monthly figure — even though your subscriptions bill
weekly, monthly, quarterly, yearly, or on a custom cycle.

## What it does

| Tool | Purpose |
|---|---|
| `add_subscription` | Add a subscription/bill with any billing cycle |
| `get_subscription` | Fetch one, with computed next renewal date |
| `list_subscriptions` | List all (or filter by category), soonest renewal first |
| `edit_subscription` | Update any field on an existing entry |
| `cancel_subscription` | Soft-delete (marks inactive, keeps history) |
| `delete_subscription` | Hard delete |
| `upcoming_renewals` | "What's renewing in the next N days?" |
| `spending_summary` | Total recurring spend, normalized to monthly/yearly, by category |

Plus a `subtrack://categories` resource (editable category list) and a
`renewal_digest_prompt` prompt template that asks the assistant to write a
friendly summary of what's coming up.

The interesting engineering bit is `next_renewal_date()`: it correctly steps
forward day-based cycles (weekly/biweekly/custom) with modular arithmetic,
and calendar-based cycles (monthly/quarterly/yearly) by adding real calendar
months (so a subscription that started Jan 31st correctly lands on Feb 28th,
not "31 days later").

## Why the tools are `async def`

Every tool here is `async def`, and all SQLite access goes through
`aiosqlite` instead of the stdlib `sqlite3`. Worth understanding *why*,
since it's a common point of confusion:

- FastMCP already runs plain `def` tools in a thread pool by default, so a
  sync version of this server wouldn't literally freeze under light load.
- But `async def` + a **blocking** driver (`sqlite3`) inside it is worse
  than staying sync — FastMCP doesn't thread-offload `async def` tools (they
  run directly on the event loop), so a blocking call inside one would stall
  every other concurrent request.
- So: either keep tools `def` and let the framework thread-offload them, or
  go `async def` *and* use a genuinely async driver all the way down. This
  server does the latter, which is the more scalable pattern for a remote
  server that may see concurrent tool calls from multiple clients — it
  doesn't consume a worker thread per in-flight DB call, and it composes
  cleanly if you add other awaitable I/O later (HTTP calls, etc).
- The one exception: the tiny `categories.json` read stays plain sync. It's
  a few hundred bytes read once per call — making it "async" would mean
  adding `aiofiles` for no real concurrency benefit.

## Project structure

```
subtrack-mcp/
├── server.py          # the whole server — module-level `mcp` object
├── pyproject.toml     # project metadata + deps (managed by uv)
├── uv.lock            # locked, reproducible dependency versions
├── .python-version    # pins the Python version uv uses
├── .gitignore
└── README.md
```

`categories.json` and `subscriptions.db` are **not** committed — `server.py`
creates them automatically on first run (see `init_categories()` /
`init_db()`). If you want your own fixed category list to survive redeploys,
remove `categories.json` from `.gitignore` and commit your edited version.

## Run it locally (uv)

No manual venv step needed — `uv run` creates and syncs `.venv` from
`uv.lock` automatically the first time you use it.

The `__main__` block in `server.py` runs the server over **HTTP** on
`0.0.0.0:8000` by default (override with the `PORT` env var) — the same
transport FastMCP Cloud uses in production, so local testing matches
what you'll actually deploy:

```bash
uv run server.py
# Starting MCP server 'SubTrack' with transport 'http' on http://0.0.0.0:8000/mcp
```

Note: this block only runs when you execute the file directly. FastMCP
Cloud ignores it entirely — it imports the `mcp` object and serves it
itself, so nothing here affects the deployed server.

Test it with a quick client script (`uv run python client_test.py`):

```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("http://127.0.0.1:8000/mcp") as client:
        result = await client.call_tool("add_subscription", {
            "name": "Netflix", "amount": 15.99, "billing_cycle": "monthly",
            "start_date": "2026-08-05", "category": "Streaming", "subcategory": "Video"
        })
        print(result.data)

asyncio.run(main())
```

If you want to test with an MCP client that expects **stdio** instead
(e.g. wiring this into Claude Desktop for local use), run it via the
FastMCP CLI, which overrides the transport regardless of what's in
`__main__`:

```bash
uv run fastmcp run server.py:mcp --transport stdio
```

### Adding or updating dependencies

Don't hand-edit `pyproject.toml`'s dependency list — let uv manage it so
`uv.lock` stays in sync:

```bash
uv add some-package             # add a new dependency
uv add some-package --upgrade   # bump one dependency
uv lock --upgrade               # re-resolve everything to latest compatible versions
```

## Deploy to FastMCP Cloud

1. Push this folder to a GitHub repo — commit `pyproject.toml` **and**
   `uv.lock` (don't commit `.venv/`, that's gitignored).
2. Sign in at [fastmcp.cloud](https://fastmcp.cloud) with GitHub and create a new project from the repo.
3. Set the **entrypoint** to:
   ```
   server.py:mcp
   ```
4. Deploy. FastMCP Cloud auto-detects dependencies from `pyproject.toml`
   (it also understands a plain `requirements.txt`, but you don't need one
   here). You'll get a URL like `https://<project>.fastmcp.app/mcp` that any
   MCP client — including Claude, via a custom connector — can call.

### A note on storage

This server uses SQLite on local disk for simplicity, which is great for
learning and for a single-instance deployment. It is **not guaranteed to
survive a redeploy** on most managed platforms (a fresh deploy usually means
a fresh filesystem). Once you're happy with the tool logic, the natural next
step — and a good exercise for learning remote MCP servers further — is
swapping `sqlite3` for a hosted database (Turso/libSQL, Postgres via
`asyncpg`, Supabase, etc.) using an environment variable for the connection
string, set in the FastMCP Cloud dashboard (`os.getenv("DATABASE_URL")`).

## Ideas to extend it

- Add an `mcp.tool()` that emails/pushes a digest using the
  `renewal_digest_prompt` output.
- Add a `price_history` table and a tool to log price increases over time.
- Add authentication (FastMCP supports bearer-token auth) once you're ready
  to make the server private instead of open.