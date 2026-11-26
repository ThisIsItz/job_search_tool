"""Groups the same job posted once per country (e.g. Elastic posts one
opening separately for Spain, Portugal, Greece...).

Jobs are only grouped AFTER the AI filter, never at fetch time: each
country's posting can have different eligibility, so each is screened on
its own and only the passing ones are grouped.

Two postings are the same job when company and title match (ignoring case
and punctuation) AND their descriptions are nearly identical. Same title
with a different description stays separate — it may be a different team's
opening — so a duplicate may occasionally show twice, but a distinct job is
never hidden.
"""
import re
from difflib import SequenceMatcher

from app import filters

MIN_DESCRIPTION_SIMILARITY = 0.8
# Enough text to tell postings apart; keeps the comparison fast for groups of ~50.
DESCRIPTION_COMPARE_CHARS = 4000


def _normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def _comparable_description(job: dict) -> str:
    return _normalize(filters.strip_html(job.get("description") or ""))[:DESCRIPTION_COMPARE_CHARS]


def _is_same_posting(description_a: str, description_b: str) -> bool:
    if not description_a or not description_b:
        return False  # nothing to compare: keep them apart rather than guess
    matcher = SequenceMatcher(None, description_a, description_b, autojunk=False)
    return matcher.quick_ratio() >= MIN_DESCRIPTION_SIMILARITY and matcher.ratio() >= MIN_DESCRIPTION_SIMILARITY


def assign_groups(jobs: list[dict]) -> dict[str, str]:
    """Maps each job's url to its group id: the url of the group's first job
    in the given order. `jobs` need url, company, title and description."""
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}  # key -> [(group_id, description)]
    group_by_url = {}
    for job in jobs:
        key = (_normalize(job["company"]), _normalize(job["title"]))
        description = _comparable_description(job)
        candidates = groups.setdefault(key, [])
        group_id = next((gid for gid, desc in candidates if _is_same_posting(desc, description)), None)
        if group_id is None:
            group_id = job["url"]
            candidates.append((group_id, description))
        group_by_url[job["url"]] = group_id
    return group_by_url
