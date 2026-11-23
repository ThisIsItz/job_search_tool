"""Deterministic filters: drop only what's obviously out (non-technical
titles via exclusion keywords, stale postings) before ai_filter.py reads
the rest. The keyword lists live in `filters.yaml` at the repo root.

Design note (2026-11-23): location/stack/role-type keyword rules used to
live here too, but word matching dropped good jobs whose wording it didn't
know ("Remoto", "Spain", "Junior UX/UI Designer", "SDE II"). ai_filter.py
answers those questions from the full posting, so they moved there.
"""
import html
import re
from datetime import datetime, timezone

import yaml

FILTERS_CONFIG_PATH = "filters.yaml"


def _load_config(path: str = FILTERS_CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


_config = _load_config()

PRIORITY_TITLE_KEYWORDS = _config["priority_title_keywords"]
TITLE_ALLOW_KEYWORDS = _config["title_allow_keywords"]
EXCLUSION_KEYWORDS = _config["exclusion_keywords"]
TRUNCATED_DESCRIPTION_MIN_CHARS = _config["truncated_description_min_chars"]
MAX_POSTING_AGE_DAYS = _config["max_posting_age_days"]

def _compile_keyword_alternation(keywords: list[str], config_key: str) -> re.Pattern:
    # An empty keyword list joins to "" and compiles to "()", which matches
    # the empty string at every position — .search() would then match ANY
    # title, silently turning an allowlist into "allow everything" and an
    # exclusion list into "exclude everything". Fail loudly instead.
    if not keywords:
        raise ValueError(
            f"filters.yaml's '{config_key}' is empty — this would silently "
            "match every title instead of none. Add at least one keyword."
        )
    return re.compile("(" + "|".join(re.escape(k) for k in keywords) + ")", re.IGNORECASE)


_priority_re = _compile_keyword_alternation(PRIORITY_TITLE_KEYWORDS, "priority_title_keywords")
_title_allow_re = _compile_keyword_alternation(TITLE_ALLOW_KEYWORDS, "title_allow_keywords")
_exclusion_re = _compile_keyword_alternation(EXCLUSION_KEYWORDS, "exclusion_keywords")

_html_tag_re = re.compile(r"<[^>]+>")


def strip_html(html_or_text: str) -> str:
    """Cheap HTML-to-text: good enough for keyword matching, not for display."""
    if not html_or_text:
        return ""
    # Unescape twice: some ATS APIs (e.g. Greenhouse) entity-escape the markup itself, sometimes doubly.
    unescaped = html.unescape(html.unescape(html_or_text))
    return _html_tag_re.sub(" ", unescaped).replace("\xa0", " ")


def looks_truncated(description: str) -> bool:
    """True if `description` looks like a short aggregator teaser rather
    than a full job posting — either it visibly cuts off mid-sentence, or
    it's just too short to contain real requirements/responsibilities.

    Shared by aggregator_clients.py (decide whether it's worth the extra
    request to fetch a full JD) and ai_evaluate.py (fall back to telling
    the model the description is partial, for the cases a full-JD fetch
    still couldn't recover — blocked site, dead link, genuinely short
    posting)."""
    text = strip_html(description or "").strip()
    if not text:
        return True
    if text.endswith("…") or text.endswith("...") or text.rstrip().endswith(".."):
        return True
    return len(text) < TRUNCATED_DESCRIPTION_MIN_CHARS


def title_is_excluded(title: str) -> bool:
    """True for titles that are obviously not a fit (sales, marketing,
    nursing...). An unambiguous engineering/design keyword overrides it, so
    "Software Engineer, Automated Marketing" isn't dropped over "marketing"."""
    title = title or ""
    return bool(_exclusion_re.search(title)) and not _priority_re.search(title)


def title_looks_technical(title: str) -> bool:
    """Only gates whether a fetcher spends an extra request on a job's full
    description — never whether the job is kept (that's ai_filter.py's call)."""
    title = title or ""
    return bool(_title_allow_re.search(title)) and not title_is_excluded(title)


def posting_is_too_old(posted_at: str | None) -> bool:
    """True if `posted_at` (ISO 8601, aware or naive) is older than
    MAX_POSTING_AGE_DAYS. Unknown/unparseable dates get the benefit of the
    doubt (False) rather than being rejected."""
    if not posted_at:
        return False
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - dt).days
    return age_days > MAX_POSTING_AGE_DAYS


def passes_filters(job: dict) -> bool:
    """Only drops what's obviously out: a non-technical title or a stale
    posting. Location, role type, seniority and stack are left to
    ai_filter.py, which reads the whole posting instead of matching words."""
    if title_is_excluded(job.get("title", "")):
        return False
    if posting_is_too_old(job.get("posted_at")):
        return False
    return True
