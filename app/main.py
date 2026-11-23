"""Orchestrator: fetch -> filter -> dedup -> data/seen_jobs.sqlite3 -> AI filter

Pulls from two kinds of sources:
  - companies.yaml   — one ATS board per known company (precise, no noise)
  - aggregators.yaml — keyword+location search across many employers at
                        once (broader reach, more noise — filters.py earns
                        its keep here)

Runs the free AI filter (ai_filter.py) on the new jobs at the end, when
GEMINI_API_KEY is set. The paid Claude evaluation (ai_evaluate.py) stays a
separate, opt-in step.

Usage (run from the repo root):
    python -m app.main                       # everything: companies + aggregators
    python -m app.main --company affirm      # just one company, for debugging
    python -m app.main --skip-aggregators    # companies.yaml only
    python -m app.main --skip-companies      # aggregators.yaml only
    python -m app.main --skip-ai-filter      # don't run the AI filter afterwards

Or `make run` for an interactive prompt instead of remembering flags.
"""
import argparse
import os
import sys

import yaml
from dotenv import load_dotenv

from app import aggregator_clients
from app import ai_filter
from app import ats_clients
from app import dedup
from app import discover_companies
from app import filters
load_dotenv()


def load_yaml_list(path: str, key: str) -> list[dict]:
    with open(path) as f:
        return yaml.safe_load(f)[key]


def process_jobs(jobs: list[dict], conn) -> list[dict]:
    """Apply filters + dedup to a batch of jobs from one source, marking
    every newly-seen job (whether it passed the content filters or not) so
    we never re-fetch/re-consider it on a future run. The full record
    (including the JD description) is always saved to job_details — see
    dedup.save_details."""
    kept = []
    for job in jobs:
        if not dedup.is_new(conn, job):
            continue
        dedup.mark_seen(conn, job)
        passed = filters.passes_filters(job)
        dedup.save_details(conn, job, passed)
        if passed:
            kept.append(job)
    return kept


def run(companies: list[dict], aggregators: list[dict]) -> list[dict]:
    all_fetched = 0
    all_candidates = []

    with dedup.connect() as conn:
        for company in companies:
            try:
                jobs = ats_clients.fetch_company(company)
            except Exception as e:
                print(f"[WARN] {company['name']}: fetch failed — {e}", file=sys.stderr)
                continue
            all_fetched += len(jobs)
            kept = process_jobs(jobs, conn)
            all_candidates.extend(kept)
            print(f"{company['name']:40s} fetched={len(jobs):4d}  new_candidates={len(kept):3d}")

        for aggregator in aggregators:
            try:
                jobs = aggregator_clients.fetch_aggregator(aggregator)
            except Exception as e:
                print(f"[WARN] {aggregator['name']}: fetch failed — {e}", file=sys.stderr)
                continue
            all_fetched += len(jobs)
            kept = process_jobs(jobs, conn)
            all_candidates.extend(kept)
            print(f"{aggregator['name']:40s} fetched={len(jobs):4d}  new_candidates={len(kept):3d}")

    print(f"\nTotal fetched: {all_fetched}  |  Total new candidates after filters+dedup: {len(all_candidates)}")
    return all_candidates


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", help="Only run a single company slug, for debugging")
    parser.add_argument("--skip-aggregators", action="store_true")
    parser.add_argument("--skip-companies", action="store_true")
    parser.add_argument("--skip-discovery", action="store_true",
                         help="Don't auto-append newly-resolved Greenhouse/Lever companies to companies.yaml")
    parser.add_argument("--skip-ai-filter", action="store_true", help="Don't run the AI filter after fetching")
    args = parser.parse_args()

    companies = [] if args.skip_companies else load_yaml_list("companies.yaml", "companies")
    aggregators = [] if args.skip_aggregators else load_yaml_list("aggregators.yaml", "aggregators")

    if args.company:
        companies = [c for c in companies if c["slug"] == args.company]
        aggregators = []
        if not companies:
            sys.exit(f"No company with slug '{args.company}' in companies.yaml")

    run(companies, aggregators)

    # Any Adzuna result whose full JD we fetched via a real Greenhouse/Lever
    # API call (see aggregator_clients.fetch_full_description) is a
    # verified-working (ats, slug) — worth tracking directly going forward
    # so future runs get that company's WHOLE board, not just whatever
    # Adzuna happened to surface this run.
    if not args.skip_discovery:
        added = discover_companies.append_new_companies(aggregator_clients.DISCOVERED_COMPANIES)
        if not added:
            print(f"No new companies were added to companies.yaml")
        if added:
            print(f"\nDiscovered {len(added)} new compan{'y' if len(added) == 1 else 'ies'} "
                  f"via Adzuna — appended to companies.yaml:")
            for c in added:
                print(f"  + {c['name']} ({c['ats']}: {c['slug']})")

    if args.skip_ai_filter:
        sys.exit(0)
    if not os.environ.get("GEMINI_API_KEY"):
        print("\nAI filter skipped: GEMINI_API_KEY not set.")
        sys.exit(0)
    print()
    try:
        ai_filter.run()
    except Exception as e:
        sys.exit(f"AI filter failed (fetched jobs are saved; retry with `python -m app.ai_filter`): {e}")
