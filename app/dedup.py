"""SQLite-backed local datastore: dedup tracking + full job details.

Two dedup keys, matching the approach from the original post:
  1. exact job URL
  2. normalized (company, title) pair — catches reposts under a new URL

A job is only "new" if neither key has been seen before. Seen jobs are
recorded regardless of whether they passed the content filters, so we
never re-fetch/re-consider the same posting on a future run.

`job_details` holds the full record (including the JD description text)
for every job we've ever seen, keyed by URL — this is where descriptions
live (rather than in a CSV). Rationale (2026-08-11): a full JD is
one to several KB of HTML/text; dumping that into a CSV column makes the
file unreadable in Excel/Sheets and defeats the point of the CSV being a
quick human-scannable list. Query this table directly (see
`get_details_by_url` / `iter_all_details`) when you want to see why a job
was included or excluded, or once ai_evaluate.py exists, to feed the JD
into the Haiku prompt without re-fetching it.
"""
import os
import re
import sqlite3
from contextlib import contextmanager

DB_PATH = "data/seen_jobs.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE,
    company_title_key TEXT,
    company TEXT,
    title TEXT,
    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_company_title_key ON seen_jobs(company_title_key);

CREATE TABLE IF NOT EXISTS job_details (
    url TEXT PRIMARY KEY,
    company TEXT,
    title TEXT,
    location TEXT,
    posted_at TEXT,
    description TEXT,
    passed_filters INTEGER,
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    source TEXT,
    salary_min REAL,
    salary_max REAL,
    salary_currency TEXT,
    apply_url TEXT
);

CREATE TABLE IF NOT EXISTS ai_evaluations (
    url TEXT PRIMARY KEY,
    match_score INTEGER,
    recommendation TEXT,
    genuine_gaps TEXT,
    transferable_strengths TEXT,
    risk_factors TEXT,
    model TEXT,
    evaluated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (url) REFERENCES job_details(url)
);

-- Cheap yes/no-style screening (ai_filter.py, Gemini Decisions API)
-- that runs between filters.py and ai_evaluate.py. `answers` is the raw
-- per-question JSON so a rejection can be audited later.
CREATE TABLE IF NOT EXISTS ai_filter (
    url TEXT PRIMARY KEY,
    passed INTEGER,
    rejection_reason TEXT,
    answers TEXT,
    model TEXT,
    evaluated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    group_id TEXT,  -- same job posted in several countries share one (see app/duplicates.py)
    FOREIGN KEY (url) REFERENCES job_details(url)
);

-- Jobs already sent to you by notify_telegram.py, so each is sent once.
CREATE TABLE IF NOT EXISTS notified_jobs (
    url TEXT PRIMARY KEY,
    notified_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (url) REFERENCES job_details(url)
);

-- Your own tracking, separate from the AI's recommendation. The AI's
-- apply/consider/skip is a suggestion made before applying; my_status is
-- what actually happened (applied/interview/rejected/skipped/silence) —
-- deliberately a different vocabulary so the two never collide in the UI.
CREATE TABLE IF NOT EXISTS user_status (
    url TEXT PRIMARY KEY,
    my_status TEXT,
    notes TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (url) REFERENCES job_details(url)
);
"""

MY_STATUS_VALUES = ["applied", "interview", "rejected", "skipped", "silence"]


def _normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def make_company_title_key(company: str, title: str, location: str = "") -> str:
    # location is part of the key so two genuinely distinct postings with
    # the same title at the same company (e.g. "Backend Engineer" open in
    # both Toronto and Vancouver) aren't treated as a repost of each other.
    return f"{_normalize(company)}::{_normalize(title)}::{_normalize(location)}"


def _migrate(conn) -> None:
    """SCHEMA's CREATE TABLE IF NOT EXISTS only handles brand-new DBs —
    columns added later need an explicit ALTER TABLE for a DB file that
    already exists (e.g. `source`, added 2026-08-12)."""
    new_columns = {
        "job_details": {"source": "TEXT", "salary_min": "REAL", "salary_max": "REAL", "salary_currency": "TEXT",
                        "apply_url": "TEXT"},
        "ai_filter": {"group_id": "TEXT"},
    }
    for table, columns in new_columns.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, column_type in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _rename_legacy_tables(conn) -> None:
    """Runs before SCHEMA, which would otherwise create an empty table under
    the new name and strand the old rows (ai_prefilter -> ai_filter, 2026-11-23)."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "ai_prefilter" in tables and "ai_filter" not in tables:
        conn.execute("ALTER TABLE ai_prefilter RENAME TO ai_filter")


@contextmanager
def connect(db_path: str = DB_PATH):
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    _rename_legacy_tables(conn)
    conn.executescript(SCHEMA)
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def is_new(conn, job: dict) -> bool:
    key = make_company_title_key(job["company"], job["title"], job.get("location", ""))
    cur = conn.execute(
        "SELECT 1 FROM seen_jobs WHERE url = ? OR company_title_key = ? LIMIT 1",
        (job["url"], key),
    )
    return cur.fetchone() is None


def mark_seen(conn, job: dict) -> None:
    key = make_company_title_key(job["company"], job["title"], job.get("location", ""))
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs (url, company_title_key, company, title) "
        "VALUES (?, ?, ?, ?)",
        (job["url"], key, job["company"], job["title"]),
    )


def save_details(conn, job: dict, passed_filters: bool) -> None:
    """Store the full job record (incl. description) keyed by URL. Called
    for every job we ever see — pass or fail — so you can audit filter
    decisions later without re-fetching anything."""
    conn.execute(
        "INSERT OR REPLACE INTO job_details "
        "(url, company, title, location, posted_at, description, passed_filters, source, "
        "salary_min, salary_max, salary_currency, apply_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job.get("url", ""),
            job.get("company", ""),
            job.get("title", ""),
            job.get("location", ""),
            job.get("posted_at"),
            job.get("description", ""),
            1 if passed_filters else 0,
            job.get("source", ""),
            job.get("salary_min"),
            job.get("salary_max"),
            job.get("salary_currency"),
            job.get("apply_url"),
        ),
    )


def get_details_by_url(conn, url: str) -> dict | None:
    cur = conn.execute(
        "SELECT url, company, title, location, posted_at, description, passed_filters, fetched_at, source "
        "FROM job_details WHERE url = ?",
        (url,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    keys = ["url", "company", "title", "location", "posted_at", "description", "passed_filters", "fetched_at", "source"]
    return dict(zip(keys, row))


def distinct_companies(conn) -> list[str]:
    """Every distinct company name we've ever seen in job_details — the
    input for try_companies_across_ats.py, which checks which of these
    aren't in companies.yaml yet and tries to guess their ATS slug
    directly, since the Adzuna redirect_url path (discover_companies.py)
    turned out to almost never leave adzuna.* for real queries (see
    diagnose_adzuna_redirects.py's 2026-08-12 finding: 25/25 samples
    stayed on Adzuna)."""
    cur = conn.execute("SELECT DISTINCT company FROM job_details WHERE company != '' ORDER BY company")
    return [row[0] for row in cur.fetchall()]


def iter_filtered_out(conn):
    """Every job we've ever stored that didn't pass the filters at fetch
    time — the input for refilter.py, which re-runs the CURRENT filters.py
    against them without re-hitting the ATS/aggregator APIs."""
    cur = conn.execute(
        "SELECT url, company, title, location, posted_at, description "
        "FROM job_details WHERE passed_filters = 0"
    )
    keys = ["url", "company", "title", "location", "posted_at", "description"]
    for row in cur.fetchall():
        yield dict(zip(keys, row))


def iter_passed(conn):
    """Every job we've ever stored that DID pass the filters at fetch time
    — the other input for refilter.py, so a TIGHTENED filter can demote
    jobs that no longer pass, not just rescue ones that now do."""
    cur = conn.execute(
        "SELECT url, company, title, location, posted_at, description "
        "FROM job_details WHERE passed_filters = 1"
    )
    keys = ["url", "company", "title", "location", "posted_at", "description"]
    for row in cur.fetchall():
        yield dict(zip(keys, row))


def set_passed_filters(conn, url: str, passed: bool) -> None:
    """Flip a stored job's passed_filters flag in place — used by
    refilter.py when a filter change rescues (or newly drops) a job that
    was already fetched, without touching seen_jobs (still deduped) or
    forcing a re-fetch."""
    conn.execute(
        "UPDATE job_details SET passed_filters = ? WHERE url = ?",
        (1 if passed else 0, url),
    )


def get_unevaluated_candidates(conn) -> list[dict]:
    """Jobs that passed the deterministic filters but haven't been scored
    by the AI evaluation step yet — the input queue for ai_evaluate.py.
    Jobs ai_filter.py rejected are skipped; jobs it hasn't seen yet are
    kept, so ai_evaluate.py still works without a Gemini key."""
    cur = conn.execute(
        "SELECT jd.url, jd.company, jd.title, jd.location, jd.posted_at, jd.description "
        "FROM job_details jd "
        "LEFT JOIN ai_evaluations ae ON jd.url = ae.url "
        "LEFT JOIN ai_filter ap ON jd.url = ap.url "
        "WHERE jd.passed_filters = 1 AND ae.url IS NULL AND (ap.passed IS NULL OR ap.passed = 1)"
    )
    keys = ["url", "company", "title", "location", "posted_at", "description"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def get_ai_filter_queue(conn, include_screened: bool = False) -> list[dict]:
    """Jobs that passed the deterministic filters and haven't been through
    ai_filter.py yet — its input queue. include_screened re-queues the
    already-screened ones too, for after its rules change."""
    cur = conn.execute(
        "SELECT jd.url, jd.company, jd.title, jd.location, jd.posted_at, jd.description "
        "FROM job_details jd "
        "LEFT JOIN ai_filter ap ON jd.url = ap.url "
        "WHERE jd.passed_filters = 1 AND (? OR ap.url IS NULL)",
        (include_screened,),
    )
    keys = ["url", "company", "title", "location", "posted_at", "description"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def get_notification_queue(conn, max_pending_days: int) -> list[dict]:
    """Jobs that passed both filters.py and ai_filter.py and haven't been
    sent by notify_telegram.py yet, in send order: jobs found since the last
    digest first, then older unsent ones, each group by best stack fit.
    Unsent jobs fetched more than max_pending_days ago are left out (they
    stay on the web board)."""
    cur = conn.execute(
        "SELECT jd.url, jd.company, jd.title, jd.location, jd.source, jd.apply_url, "
        "       jd.salary_min, jd.salary_max, jd.salary_currency, "
        "       json_extract(af.answers, '$.stack_fit.score') AS stack_fit, "
        "       json_extract(af.answers, '$.work_mode.choice'), "
        "       json_extract(af.answers, '$.seniority.choice'), "
        "       json_extract(af.answers, '$.role_type.choice'), "
        "       jd.fetched_at > COALESCE((SELECT MAX(notified_at) FROM notified_jobs), '') AS is_new, "
        "       COALESCE(af.group_id, jd.url), "
        "       EXISTS (SELECT 1 FROM ai_filter af2 JOIN notified_jobs nj2 ON nj2.url = af2.url "
        "               WHERE af2.group_id = af.group_id) "
        "FROM job_details jd "
        "JOIN ai_filter af ON jd.url = af.url "
        "LEFT JOIN notified_jobs nj ON jd.url = nj.url "
        "WHERE jd.passed_filters = 1 AND af.passed = 1 AND nj.url IS NULL "
        "  AND jd.fetched_at >= datetime('now', ?) "
        "ORDER BY is_new DESC, stack_fit DESC",
        (f"-{max_pending_days} days",),
    )
    keys = ["url", "company", "title", "location", "source", "apply_url", "salary_min", "salary_max",
            "salary_currency", "stack_fit", "work_mode", "seniority", "role_type", "is_new",
            "group_id", "group_already_sent"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def mark_notified(conn, urls: list[str]) -> None:
    conn.executemany("INSERT OR IGNORE INTO notified_jobs (url) VALUES (?)", [(url,) for url in urls])


def get_ai_filter_passes(conn) -> list[dict]:
    """Every job that passed both filters, oldest first — the input for
    app/duplicates.py, so a group's id stays the url of its first posting."""
    cur = conn.execute(
        "SELECT jd.url, jd.company, jd.title, jd.description FROM job_details jd "
        "JOIN ai_filter af ON jd.url = af.url "
        "WHERE jd.passed_filters = 1 AND af.passed = 1 ORDER BY jd.fetched_at, jd.url"
    )
    return [dict(zip(["url", "company", "title", "description"], row)) for row in cur.fetchall()]


def save_duplicate_groups(conn, group_by_url: dict[str, str]) -> None:
    conn.executemany("UPDATE ai_filter SET group_id = ? WHERE url = ?",
                     [(group_id, url) for url, group_id in group_by_url.items()])


def save_ai_filter(conn, url: str, rejection_reason: str | None, answers_json: str, model: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ai_filter (url, passed, rejection_reason, answers, model) "
        "VALUES (?, ?, ?, ?, ?)",
        (url, 0 if rejection_reason else 1, rejection_reason, answers_json, model),
    )


def save_evaluation(conn, url: str, evaluation: dict, model: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ai_evaluations "
        "(url, match_score, recommendation, genuine_gaps, transferable_strengths, risk_factors, model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            url,
            evaluation.get("match_score"),
            evaluation.get("recommendation"),
            evaluation.get("genuine_gaps"),
            evaluation.get("transferable_strengths"),
            evaluation.get("risk_factors"),
            model,
        ),
    )


def save_user_status(conn, url: str, my_status: str | None, notes: str | None) -> None:
    """my_status should be one of MY_STATUS_VALUES or None/'' to clear it.
    Not validated strictly here (the web UI constrains it via a <select>)
    so a hand-edited import file with a typo doesn't hard-fail the import."""
    conn.execute(
        "INSERT INTO user_status (url, my_status, notes, updated_at) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(url) DO UPDATE SET my_status=excluded.my_status, "
        "notes=excluded.notes, updated_at=CURRENT_TIMESTAMP",
        (url, my_status or None, notes or None),
    )


def get_user_status(conn, url: str) -> dict | None:
    cur = conn.execute("SELECT url, my_status, notes, updated_at FROM user_status WHERE url = ?", (url,))
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(["url", "my_status", "notes", "updated_at"], row))
