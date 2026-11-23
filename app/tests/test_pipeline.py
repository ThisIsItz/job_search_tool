"""Sanity test for filters.py + dedup.py, using real sample data.

Since 2026-11-23 filters.py only drops obviously non-technical titles and
stale postings; location, stack and role type are ai_filter.py's call, so
jobs like "Remote US" or a Java-only JD now pass here on purpose.
Run with: python -m app.tests.test_pipeline
"""
import os
from app import filters
from app import dedup

SAMPLE_JOBS = [
    # PASS: engineering title
    {"company": "Affirm", "title": "Senior Software Engineer, Backend (Batch Infrastructure)",
     "location": "Remote Canada", "url": "https://job-boards.greenhouse.io/affirm/jobs/1111",
     "description": "We use Python, FastAPI and React."},
    # DROP: excluded title keyword (compliance)
    {"company": "Affirm", "title": "Compliance Lead, Canada",
     "location": "Remote Canada", "url": "https://job-boards.greenhouse.io/affirm/jobs/7788916003",
     "description": ""},
    # PASS: location is the AI filter's call now
    {"company": "Affirm", "title": "Staff Software Engineer",
     "location": "Remote US", "url": "https://job-boards.greenhouse.io/affirm/jobs/2222",
     "description": ""},
    {"company": "Affirm", "title": "Analytics Engineer II",
     "location": "Remote Poland", "url": "https://job-boards.greenhouse.io/affirm/jobs/7764109003",
     "description": ""},
    # DROP: excluded title keywords (marketing, talent brand)
    {"company": "Affirm", "title": "Marketing Operations Manager",
     "location": "Remote Canada", "url": "https://job-boards.greenhouse.io/affirm/jobs/9999",
     "description": ""},
    {"company": "Affirm", "title": "Senior Manager, Talent Brand",
     "location": "Remote Canada", "url": "https://job-boards.greenhouse.io/affirm/jobs/9998",
     "description": ""},
    # PASS: on-site location, left to the AI filter
    {"company": "Snorkel AI", "title": "Software Engineer — Backend",
     "location": "Calgary, AB", "url": "https://job-boards.greenhouse.io/snorkelai/jobs/4911972004",
     "description": "Backend role, Python and Go."},
    # DROP: excluded keyword (nurse)
    {"company": "Some Co", "title": "Occupational Health Nurse",
     "location": "Remote Canada", "url": "https://example.com/jobs/9999",
     "description": ""},
    # PASS: Java-only JD — stack fit is the AI filter's call now
    {"company": "BigCorp", "title": "Senior Software Engineer",
     "location": "Remote Canada", "url": "https://example.com/jobs/8888",
     "description": "5+ years of Java and Spring Boot required. Experience with Kafka a plus."},
    {"company": "DualStackCo", "title": "Backend Engineer",
     "location": "Remote Canada", "url": "https://example.com/jobs/7777",
     "description": "Our platform is a mix of Java services and a newer Python/FastAPI stack."},
    # Duplicate of the first PASS entry by URL -> should be filtered by dedup on 2nd pass
    {"company": "Affirm", "title": "Senior Software Engineer, Backend (Batch Infrastructure)",
     "location": "Remote Canada", "url": "https://job-boards.greenhouse.io/affirm/jobs/1111",
     "description": "We use Python, FastAPI and React."},
    # Repost under a new URL, same company+title -> should be caught by company+title dedup
    {"company": "Affirm", "title": "Senior Software Engineer, Backend (Batch Infrastructure)",
     "location": "Remote Canada", "url": "https://job-boards.greenhouse.io/affirm/jobs/3333-repost",
     "description": "We use Python, FastAPI and React."},
    {"company": "AltaML", "title": "Intermediate Full Stack Software Engineer",
     "location": "Edmonton, Calgary", "url": "https://jobs.lever.co/altaml/22d02404-b9c9-4b77-9448-add55d961444",
     "description": "Python and React experience preferred."},
    # PASS: "Software Engineer" is a priority keyword, so "marketing" doesn't exclude it
    {"company": "Warner Music Group", "title": "Software Engineer, Automated Marketing",
     "location": "Alberta, Canada", "url": "https://www.adzuna.ca/details/5702928490",
     "description": "Build automated marketing tooling. Python and React."},
    # DROP: stale posting
    {"company": "OldCo", "title": "Frontend Engineer", "location": "Remote",
     "url": "https://example.com/jobs/old", "description": "", "posted_at": "2020-01-01T00:00:00Z"},
]

TEST_DB = "data/test_seen_jobs.sqlite3"


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    print("=== Filter results ===")
    filtered = []
    for job in SAMPLE_JOBS:
        ok = filters.passes_filters(job)
        print(f"{'PASS' if ok else 'DROP':5s} | {job['company']:14s} | {job['title'][:50]:50s} | {job['location']}")
        if ok:
            filtered.append(job)

    print(f"\n{len(filtered)}/{len(SAMPLE_JOBS)} passed the title-exclusion + age filters\n")

    print("=== Dedup results (processing filtered jobs in order) ===")
    kept = []
    with dedup.connect(TEST_DB) as conn:
        for job in filtered:
            if dedup.is_new(conn, job):
                dedup.mark_seen(conn, job)
                kept.append(job)
                print(f"NEW  | {job['company']:14s} | {job['title'][:50]:50s} | {job['url']}")
            else:
                print(f"DUPE | {job['company']:14s} | {job['title'][:50]:50s} | {job['url']}")

    print(f"\nFinal candidates after filters+dedup: {len(kept)}")

    # DROP: Compliance Lead, Marketing Ops Manager, Talent Brand, Nurse, stale posting.
    assert len(filtered) == 10, f"expected 10 to pass the filters, got {len(filtered)}"
    # Minus the URL duplicate and the company+title repost.
    assert len(kept) == 8, f"expected 8 unique candidates after dedup, got {len(kept)}"

    assert filters.title_is_excluded("Marketing Operations Manager")
    assert filters.title_is_excluded("Sales Engineer")  # priority list shouldn't rescue this
    assert not filters.title_is_excluded("Software Engineer, Automated Marketing")
    assert not filters.title_is_excluded("Junior UX/UI Designer")
    assert filters.title_looks_technical("Desarrollador Frontend")
    assert not filters.title_looks_technical("Marketing Operations Manager")

    os.remove(TEST_DB)
    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()
