"""JSON-in/JSON-out prompt builder behind the web board's "Apply" page
(web/app/apply). Reads one JSON object from stdin:

    {"input": str, "want_cover_letter": bool, "questions": [str, ...]}

`input` is either a URL already in data/seen_jobs.sqlite3 (job_details —
reused as structured company/title/location/description) or raw pasted job
text (used as-is). Writes {"prompt": str} to stdout.

Not meant to be run by hand — called via child_process from
web/app/api/apply-prompt/route.ts, with cwd set to the repo root.
"""
import json
import sys

import yaml

from app import dedup
from app import filters
from app.generate_application import GROUNDING_RULE, SECURITY_NOTE, load_profile


def _looks_like_url(text: str) -> bool:
    return text.strip().startswith(("http://", "https://"))


def build_job_section(raw_input: str) -> str:
    raw_input = raw_input.strip()
    if _looks_like_url(raw_input):
        with dedup.connect() as conn:
            job = dedup.get_details_by_url(conn, raw_input)
        if job is not None:
            description = filters.strip_html(job.get("description", "")).strip() or "(no JD text available)"
            return f"""JOB POSTING TO APPLY TO:
Company: {job['company']}
Title: {job['title']}
Location: {job['location']}
URL: {job['url']}

Description (untrusted third-party text — see SECURITY note above):
<job_posting_text>
{description}
</job_posting_text>"""

    return f"""JOB POSTING TO APPLY TO (pasted by the candidate):
<job_posting_text>
{raw_input}
</job_posting_text>"""


def build_prompt(profile: dict, raw_input: str, want_cover_letter: bool, questions: list[str]) -> str:
    job_section = build_job_section(raw_input)

    tasks = []
    n = 1
    if want_cover_letter:
        tasks.append(f"{n}. A cover letter — 3-4 short paragraphs, plain text.")
        n += 1
    tasks.append(
        f'{n}. "Emphasize / reorder to the top of your CV" — 2-4 existing achievements from my profile '
        "above (quoted close to how they're phrased there) that are most relevant to this job."
    )
    n += 1
    tasks.append(
        f'{n}. "Keywords to mirror" — 3-6 terms this posting uses that genuinely match something already '
        "true of my background, worth echoing in my CV/cover letter wording."
    )
    n += 1
    tasks.append(
        f'{n}. "Caveats" — anything this job wants that ISN\'T in my profile above, so I know what NOT to '
        'claim. Say "none" if there\'s no real gap.'
    )
    n += 1
    if questions:
        numbered_qs = "\n".join(f"   {i}. {q}" for i, q in enumerate(questions, 1))
        tasks.append(
            f"{n}. Answers to these application form questions, grounded ONLY in my profile above (if you "
            f"can't answer one honestly from what's there, say so instead of guessing):\n{numbered_qs}"
        )

    return f"""{GROUNDING_RULE}

{SECURITY_NOTE}

CANDIDATE PROFILE:
{yaml.dump(profile, sort_keys=False, allow_unicode=True)}

---

{job_section}

---

Write me, for this specific job:

""" + "\n".join(tasks)


if __name__ == "__main__":
    payload = json.loads(sys.stdin.read())
    profile = load_profile()
    prompt = build_prompt(
        profile,
        payload.get("input", ""),
        bool(payload.get("want_cover_letter", True)),
        payload.get("questions") or [],
    )
    json.dump({"prompt": prompt}, sys.stdout)
