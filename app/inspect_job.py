"""Look up a job's full stored record (including JD description) from
data/seen_jobs.sqlite3 — useful for checking why something was included or
excluded without re-fetching it from the ATS.

Usage (run from the repo root):
    python -m app.inspect_job <url>              # full record for one job
    python -m app.inspect_job --rejected [N]     # last N rejected jobs (title + why), default 20
"""
import sys

from app import dedup
from app import filters


def rejection_reasons(job: dict) -> str:
    reasons = []
    if filters.title_is_excluded(job.get("title") or ""):
        reasons.append("title")
    if filters.posting_is_too_old(job.get("posted_at")):
        reasons.append("age")
    return "+".join(reasons)


def show_one(url: str) -> None:
    with dedup.connect() as conn:
        details = dedup.get_details_by_url(conn, url)
    if details is None:
        print(f"No stored record for {url}")
        return
    for k, v in details.items():
        if k == "description":
            continue
        print(f"{k:16s}: {v}")

    if not details.get("passed_filters"):
        print(f"filtered out by: {rejection_reasons(details) or '? (filters.py changed since it was stored)'}")

    print("-" * 60)
    print(details.get("description") or "(no description stored)")


def show_rejected(limit: int = 20) -> None:
    with dedup.connect() as conn:
        cur = conn.execute(
            "SELECT company, title, location, url, posted_at FROM job_details "
            "WHERE passed_filters = 0 ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()

    for company, title, location, url, posted_at in rows:
        reasons = rejection_reasons({"title": title, "posted_at": posted_at})
        print(f"[{reasons or '?':16s}] {company:20s} | {title[:50]:50s} | {location}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if sys.argv[1] == "--rejected":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        show_rejected(n)
    else:
        show_one(sys.argv[1])