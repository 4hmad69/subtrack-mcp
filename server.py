"""
SubTrack MCP Server (async)
----------------------------
Solves a very ordinary daily-life problem: forgotten/duplicate subscriptions
and recurring bills quietly draining money each month (streaming services,
software, gym memberships, insurance, etc).

Lets an LLM (or you, via any MCP client) add subscriptions with any billing
cycle, see what's about to renew, and get spend normalized to a monthly/
yearly equivalent so weekly, quarterly, and yearly charges can be compared
fairly.

WHY ASYNC:
FastMCP will happily run plain `def` tools by dispatching each call to a
thread pool, so a sync version of this server would not technically "block"
under light load. But for a *remote* server that may see multiple concurrent
tool calls, real async I/O is the more correct and scalable pattern: it
doesn't consume a worker thread per in-flight DB call, and it composes
cleanly if you later add other awaitable I/O (HTTP calls, other async
clients, etc). So every tool here is `async def`, and the one piece of I/O
that matters — SQLite access — uses `aiosqlite` so the `await` is real, not
decorative. The tiny one-time categories.json read stays plain sync (see
note below) since making it "async" would add a dependency and complexity
for a few milliseconds of startup I/O with no concurrency to protect.

Deploy target: FastMCP Cloud (https://fastmcp.cloud)
  - The `mcp` object below MUST stay at module level (Cloud imports it as
    `server.py:mcp`).
  - The `if __name__ == "__main__"` block is only used for local testing;
    FastMCP Cloud ignores it and serves the `mcp` object over HTTP itself.
"""

import calendar
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

import aiosqlite
from fastmcp import FastMCP

DB_PATH = os.path.join(os.path.dirname(__file__), "subscriptions.db")
CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")

mcp = FastMCP("SubTrack")

DEFAULT_CATEGORIES = {
    "Streaming": ["Video", "Music", "Gaming"],
    "Software": ["Productivity", "Developer Tools", "Design", "Cloud Storage"],
    "Utilities": ["Internet", "Phone", "Electricity"],
    "Health & Fitness": ["Gym", "Apps"],
    "Finance": ["Insurance", "Banking Fees"],
    "News & Reading": ["Newspapers", "Magazines", "Newsletters"],
    "Other": [],
}

CYCLE_DAYS = {"weekly": 7, "biweekly": 14}
CYCLE_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}
VALID_CYCLES = set(CYCLE_DAYS) | set(CYCLE_MONTHS) | {"custom"}


# ---------------------------------------------------------------------------
# Startup / storage setup — runs once at import time, before any event loop
# is serving requests, so plain blocking sqlite3 is fine here.
# ---------------------------------------------------------------------------

def init_categories():
    """Create a default categories.json if one wasn't pushed to the repo."""
    if not os.path.exists(CATEGORIES_PATH):
        with open(CATEGORIES_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CATEGORIES, f, indent=2)


def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                amount REAL NOT NULL,
                billing_cycle TEXT NOT NULL,
                interval_days INTEGER,
                start_date TEXT NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_subs_active ON subscriptions(active)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_subs_category ON subscriptions(category)")


init_categories()
init_db()


# ---------------------------------------------------------------------------
# Pure helpers (no I/O — safe to call directly from async tools)
# ---------------------------------------------------------------------------

def load_categories():
    """Small local JSON read (a few hundred bytes). Left synchronous on
    purpose: it's a one-shot local file read that finishes in microseconds,
    so offloading it to aiofiles/a thread would add complexity without any
    real concurrency benefit."""
    with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_category(category: str, subcategory: str = ""):
    """Returns an error message string if invalid, else None."""
    cats = load_categories()
    if category not in cats:
        return f"Invalid category '{category}'. Valid categories: {', '.join(cats.keys())}"
    if subcategory and cats[category] and subcategory not in cats[category]:
        return (
            f"Invalid subcategory '{subcategory}' for category '{category}'. "
            f"Valid subcategories: {', '.join(cats[category])}"
        )
    return None


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def add_months(d: date, months: int) -> date:
    """Add calendar months to a date, clamping the day to the target month's length."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_renewal_date(start: date, billing_cycle: str, interval_days: Optional[int],
                       today: Optional[date] = None) -> date:
    """Compute the next billing date on or after `today`."""
    today = today or date.today()
    if start >= today:
        return start

    if billing_cycle in CYCLE_DAYS:
        step = CYCLE_DAYS[billing_cycle]
    elif billing_cycle == "custom":
        step = interval_days or 30
    else:
        step = None

    if step is not None:
        diff = (today - start).days
        periods = diff // step
        candidate = start + timedelta(days=periods * step)
        if candidate < today:
            candidate += timedelta(days=step)
        return candidate

    # Month-based cycles (monthly / quarterly / yearly): day-count varies per
    # month, so step forward in calendar months instead of raw days.
    months_per_cycle = CYCLE_MONTHS[billing_cycle]
    total_months = (today.year - start.year) * 12 + (today.month - start.month)
    cycles_elapsed = max(total_months // months_per_cycle, 0)
    candidate = add_months(start, cycles_elapsed * months_per_cycle)
    while candidate < today:
        candidate = add_months(candidate, months_per_cycle)
    return candidate


def monthly_equivalent(amount: float, billing_cycle: str, interval_days: Optional[int]) -> float:
    """Normalize any billing cycle to an average monthly cost, for fair comparison."""
    if billing_cycle == "weekly":
        return amount * (52 / 12)
    if billing_cycle == "biweekly":
        return amount * (26 / 12)
    if billing_cycle == "monthly":
        return amount
    if billing_cycle == "quarterly":
        return amount / 3
    if billing_cycle == "yearly":
        return amount / 12
    if billing_cycle == "custom":
        step = interval_days or 30
        return amount * (30.44 / step)
    return amount


def enrich(sub: dict) -> dict:
    """Attach computed next_renewal / days_until_renewal / monthly_equivalent."""
    start = parse_date(sub["start_date"])
    nxt = next_renewal_date(start, sub["billing_cycle"], sub["interval_days"])
    sub["next_renewal"] = nxt.isoformat()
    sub["days_until_renewal"] = (nxt - date.today()).days
    sub["monthly_equivalent"] = round(
        monthly_equivalent(sub["amount"], sub["billing_cycle"], sub["interval_days"]), 2
    )
    return sub


async def get_db():
    """Open an aiosqlite connection with dict-like row access."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


# ---------------------------------------------------------------------------
# Tools — all async def, all DB access genuinely awaited via aiosqlite
# ---------------------------------------------------------------------------

@mcp.tool()
async def add_subscription(name: str, amount: float, billing_cycle: str, start_date: str,
                            category: str, subcategory: str = "",
                            interval_days: Optional[int] = None, note: str = "") -> dict:
    """
    Add a new recurring subscription or bill.

    billing_cycle: one of "weekly", "biweekly", "monthly", "quarterly", "yearly", "custom".
    If billing_cycle is "custom", interval_days must also be given (e.g. every 45 days).
    start_date must be YYYY-MM-DD: the date this subscription first billed (or will bill).
    """
    if billing_cycle not in VALID_CYCLES:
        return {"status": "error",
                "message": f"Invalid billing_cycle '{billing_cycle}'. Valid: {', '.join(sorted(VALID_CYCLES))}"}
    if billing_cycle == "custom" and (not interval_days or interval_days <= 0):
        return {"status": "error", "message": "interval_days (positive integer) is required when billing_cycle is 'custom'"}

    try:
        parse_date(start_date)
    except ValueError:
        return {"status": "error", "message": "start_date must be in YYYY-MM-DD format"}

    error = validate_category(category, subcategory)
    if error:
        return {"status": "error", "message": error}

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO subscriptions
               (name, amount, billing_cycle, interval_days, start_date, category, subcategory, note, active, created_at)
               VALUES (?,?,?,?,?,?,?,?,1,?)""",
            (name, amount, billing_cycle, interval_days, start_date, category, subcategory, note,
             datetime.utcnow().isoformat()),
        )
        await db.commit()
        return {"status": "ok", "id": cur.lastrowid}


@mcp.tool()
async def get_subscription(id: int) -> dict:
    """Get a single subscription by id, including its computed next renewal date."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM subscriptions WHERE id = ?", (id,))
        row = await cur.fetchone()
        if row is None:
            return {"status": "error", "message": f"No subscription found with id {id}"}
        return enrich(dict(row))


@mcp.tool()
async def list_subscriptions(active_only: bool = True, category: Optional[str] = None) -> list:
    """
    List subscriptions, each enriched with next_renewal, days_until_renewal, and
    monthly_equivalent cost. Sorted by soonest renewal first.
    """
    query = "SELECT * FROM subscriptions WHERE 1=1"
    params = []
    if active_only:
        query += " AND active = 1"
    if category:
        query += " AND category = ?"
        params.append(category)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        rows = [enrich(dict(r)) for r in await cur.fetchall()]

    rows.sort(key=lambda r: r["next_renewal"])
    return rows


@mcp.tool()
async def edit_subscription(id: int, name: Optional[str] = None, amount: Optional[float] = None,
                             billing_cycle: Optional[str] = None, interval_days: Optional[int] = None,
                             start_date: Optional[str] = None, category: Optional[str] = None,
                             subcategory: Optional[str] = None, note: Optional[str] = None) -> dict:
    """Edit an existing subscription. Only the fields provided are updated."""
    fields = {
        "name": name, "amount": amount, "billing_cycle": billing_cycle,
        "interval_days": interval_days, "start_date": start_date,
        "category": category, "subcategory": subcategory, "note": note,
    }
    updates = {k: v for k, v in fields.items() if v is not None}
    if not updates:
        return {"status": "error", "message": "No fields provided to update"}

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT category, subcategory FROM subscriptions WHERE id = ?", (id,))
        row = await cur.fetchone()
        if row is None:
            return {"status": "error", "message": f"No subscription found with id {id}"}

        if "billing_cycle" in updates and updates["billing_cycle"] not in VALID_CYCLES:
            return {"status": "error", "message": f"Invalid billing_cycle. Valid: {', '.join(sorted(VALID_CYCLES))}"}

        if "start_date" in updates:
            try:
                parse_date(updates["start_date"])
            except ValueError:
                return {"status": "error", "message": "start_date must be in YYYY-MM-DD format"}

        if "category" in updates or "subcategory" in updates:
            check_category = updates.get("category", row["category"])
            check_subcategory = updates.get("subcategory", row["subcategory"])
            error = validate_category(check_category, check_subcategory)
            if error:
                return {"status": "error", "message": error}

        set_clause = ", ".join(f"{col} = ?" for col in updates)
        values = list(updates.values()) + [id]
        await db.execute(f"UPDATE subscriptions SET {set_clause} WHERE id = ?", values)
        await db.commit()

    return {"status": "ok", "id": id, "updated_fields": list(updates.keys())}


@mcp.tool()
async def cancel_subscription(id: int) -> dict:
    """Mark a subscription as cancelled (soft delete) so it stops showing as active
    but stays in history for reporting."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE subscriptions SET active = 0 WHERE id = ?", (id,))
        await db.commit()
        if cur.rowcount == 0:
            return {"status": "error", "message": f"No subscription found with id {id}"}
    return {"status": "ok", "cancelled_id": id}


@mcp.tool()
async def delete_subscription(id: int) -> dict:
    """Permanently delete a subscription record."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM subscriptions WHERE id = ?", (id,))
        await db.commit()
        if cur.rowcount == 0:
            return {"status": "error", "message": f"No subscription found with id {id}"}
    return {"status": "ok", "deleted_id": id}


@mcp.tool()
async def upcoming_renewals(days: int = 7) -> list:
    """List active subscriptions renewing within the next N days (default 7), soonest first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM subscriptions WHERE active = 1")
        rows = [enrich(dict(r)) for r in await cur.fetchall()]

    upcoming = [r for r in rows if 0 <= r["days_until_renewal"] <= days]
    upcoming.sort(key=lambda r: r["days_until_renewal"])
    return upcoming


@mcp.tool()
async def spending_summary(by: str = "category") -> dict:
    """
    Summarize recurring spend across all active subscriptions, normalized to a
    monthly equivalent so weekly/yearly/etc costs can be compared fairly.

    by: "category" groups totals by category, "all" returns just the grand total.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM subscriptions WHERE active = 1")
        rows = [enrich(dict(r)) for r in await cur.fetchall()]

    grand_total = round(sum(r["monthly_equivalent"] for r in rows), 2)

    if by == "all":
        return {"monthly_total": grand_total, "yearly_total": round(grand_total * 12, 2),
                "active_subscriptions": len(rows)}

    totals: dict = {}
    for r in rows:
        totals[r["category"]] = totals.get(r["category"], 0) + r["monthly_equivalent"]

    by_category = [
        {"category": cat, "monthly_total": round(total, 2), "yearly_total": round(total * 12, 2)}
        for cat, total in sorted(totals.items())
    ]

    return {
        "by_category": by_category,
        "monthly_total": grand_total,
        "yearly_total": round(grand_total * 12, 2),
        "active_subscriptions": len(rows),
    }


# ---------------------------------------------------------------------------
# Resources & prompts
# ---------------------------------------------------------------------------

@mcp.resource("subtrack://categories", mime_type="application/json")
def categories() -> str:
    """Available categories and subcategories for subscriptions.
    Sync on purpose (see load_categories note above) — a tiny local read."""
    with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
        return f.read()


@mcp.prompt()
def renewal_digest_prompt(days: int = 7) -> str:
    """Prompt template: ask the assistant to summarize upcoming renewals in plain
    English. Sync on purpose — pure string formatting, no I/O at all."""
    return (
        f"Call upcoming_renewals with days={days}, then write a short, friendly digest "
        f"summarizing what's renewing soon, the total cost, and flag anything that looks "
        f"like it could be cancelled to save money."
    )


if __name__ == "__main__":
    # Local / self-hosted testing only. FastMCP Cloud ignores this block
    # entirely — it imports the `mcp` object directly and serves it over
    # HTTP itself, so nothing here affects the Cloud deployment.
    #
    # It DOES matter if you run this file yourself (`uv run server.py`) or
    # ever self-host outside FastMCP Cloud (Docker, a VPS, Cloud Run, etc.):
    # host="0.0.0.0" makes the server reachable from outside the container/
    # machine (127.0.0.1 would only accept connections from itself), and
    # reading PORT from the environment matches how most hosts (Cloud Run,
    # Render, Fly, etc.) tell your process which port to bind.
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="http", host="0.0.0.0", port=port)