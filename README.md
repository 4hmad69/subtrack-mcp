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

## Storage: Turso (hosted libSQL)

Data lives in [Turso](https://turso.tech), a hosted, SQLite-compatible
database with a free tier — not on local disk. That's a deliberate change
from an earlier version of this project, which used a local
`subscriptions.db` file next to `server.py`. That approach worked for local
testing, but broke once deployed: FastMCP Cloud's filesystem is writable
during the build step (so the table got created fine) but mounted read-only
at runtime, so every `INSERT`/`UPDATE` failed with
`attempt to write a readonly database`. It also wouldn't have survived a
redeploy even without that error, since a fresh deploy means a fresh
filesystem. Turso fixes both problems: it's reachable over HTTPS regardless
of where the server runs, and it persists independently of any single
deploy.

Schema and SQL are unchanged from plain SQLite — only the connection layer
changed.

## Why the tools are `async def`

Every tool here is `async def`. Worth understanding *why*, since it's a
common point of confusion — and the reasoning changed slightly with the
Turso migration:

- FastMCP already runs plain `def` tools in a thread pool by default, so a
  sync version of this server wouldn't literally freeze under light load.
- But `async def` + a **blocking** call inside it is worse than staying
  sync — FastMCP doesn't thread-offload `async def` tools (they run
  directly on the event loop), so a blocking call inside one would stall
  every other concurrent request.
- The `libsql` Python client is sync (it wraps HTTP calls under the hood),
  not `async def`. So instead of calling it directly inside an `async def`
  tool — which would block the event loop for the duration of each network
  round trip to Turso — every DB call is wrapped in `asyncio.to_thread(...)`.
  That preserves the same non-blocking guarantee a native async driver would
  give, without requiring one to exist.
- The one exception: the tiny `categories.json` read stays plain sync. It's
  a few hundred bytes read once per call — making it "async" would mean
  adding `aiofiles` for no real concurrency benefit.

## Project structure

```
subtrack-mcp/
├── server.py          # the whole server — module-level `mcp` object
├── categories.json    # subscription categories (commit this — see below)
├── pyproject.toml     # project metadata + deps (managed by uv)
├── uv.lock            # locked, reproducible dependency versions
├── .python-version    # pins the Python version uv uses
├── .gitignore
└── README.md
```

`categories.json` is created automatically on first run if it's missing
(see `init_categories()`), but **commit it** rather than relying on that —
letting it auto-generate means its one-time write is exposed to the same
read-only-at-runtime risk that broke local SQLite. Remove it from
`.gitignore` and commit your (possibly edited) version so it's just part of
the deployed code, not something written at startup.

There's no local database file anymore — subscription data lives entirely
in Turso, addressed via the environment variables below.

## Run it locally (uv)

No manual venv step needed — `uv run` creates and syncs `.venv` from
`uv.lock` automatically the first time you use it.

Local runs need the same two environment variables production does (see
[Set up Turso](#set-up-turso-free-tier) below):

```bash
export TURSO_DATABASE_URL="libsql://your-database.turso.io"
export TURSO_AUTH_TOKEN="your-token"
uv run server.py
# Starting MCP server 'SubTrack' with transport 'http' on http://0.0.0.0:8000/mcp
```

(On Windows PowerShell, use `$env:TURSO_DATABASE_URL = "..."` instead of
`export`.)

Note: the `__main__` block only runs when you execute the file directly.
FastMCP Cloud ignores it entirely — it imports the `mcp` object and serves
it itself, so nothing here affects the deployed server.

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

> **Windows note:** `libsql` ships prebuilt wheels for Linux/macOS/Windows,
> but not always for every Python version the same day it's released — if
> `uv` starts compiling it from source (you'll see `Building libsql==...`),
> that requires a Rust toolchain (`winget install Rustlang.Rustup`) and can
> also fail with `Access is denied` if your project folder is inside a
> OneDrive-synced directory, since OneDrive's file locking races with
> Cargo's build output. Easiest fixes: run inside WSL instead (a Linux wheel
> is available, no build needed), or move the project outside OneDrive.

### Adding or updating dependencies

Don't hand-edit `pyproject.toml`'s dependency list — let uv manage it so
`uv.lock` stays in sync:

```bash
uv add some-package             # add a new dependency
uv add some-package --upgrade   # bump one dependency
uv add some-package --no-sync   # update pyproject.toml/uv.lock without installing locally
uv lock --upgrade               # re-resolve everything to latest compatible versions
```

`--no-sync` is useful if a package needs to build from source locally (see
the Windows note above) but you don't actually need it installed
locally — e.g. because the real deploy target (FastMCP Cloud, Linux) has a
prebuilt wheel and will never hit the same problem.

## Set up Turso (free tier)

```bash
# Install the CLI (Linux/macOS, or inside WSL on Windows —
# see https://docs.turso.tech/cli/installation for Windows/WSL steps)
curl -sSfL https://get.tur.so/install.sh | bash
turso auth login          # or `turso auth login --headless` over SSH/WSL

turso db create subtrack
turso db show subtrack --url        # → TURSO_DATABASE_URL
turso db tokens create subtrack     # → TURSO_AUTH_TOKEN
```

Keep both values somewhere safe — you'll need them for local runs (above)
and for the FastMCP Cloud deployment (below).

## Deploy to FastMCP Cloud

1. Push this folder to a GitHub repo — commit `pyproject.toml`, `uv.lock`,
   and `categories.json` (don't commit `.venv/`, that's gitignored).
2. Sign in at [fastmcp.cloud](https://fastmcp.cloud) with GitHub and create
   a new project from the repo.
3. Set the **entrypoint** to:
   ```
   server.py:mcp
   ```
4. In the project's environment variables settings, add:
   - `TURSO_DATABASE_URL`
   - `TURSO_AUTH_TOKEN`

   (from [Set up Turso](#set-up-turso-free-tier) above).
5. Deploy. FastMCP Cloud auto-detects dependencies from `pyproject.toml`.
   You'll get a URL like `https://<project>.fastmcp.app/mcp` that any MCP
   client — including Claude, via a custom connector — can call.

Any time you change code or dependencies, commit and push — FastMCP Cloud
redeploys automatically on push to `main`.

## Ideas to extend it

- Add an `mcp.tool()` that emails/pushes a digest using the
  `renewal_digest_prompt` output.
- Add a `price_history` table and a tool to log price increases over time.
- Add authentication (FastMCP supports bearer-token auth) once you're ready
  to make the server private instead of open.
