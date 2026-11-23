"""Clients for broad job aggregators (keyword+location search across many
employers), as opposed to ats_clients.py which is one board per company.

Same output shape as ats_clients.py:
    {"company": str, "title": str, "location": str, "url": str, "posted_at": str|None}

Same sandbox caveat as ats_clients.py: outbound calls to these domains were
not testable live from this cloud dev environment (network allowlist), only
via WebFetch during research. Run for real on a machine with normal internet.
"""
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime

import httpx

from app import filters

USER_AGENT = "job-search-pipeline/0.1 (personal use)"
TIMEOUT = 20.0
FULL_JD_TIMEOUT = 10.0  # shorter: this is a best-effort extra request per job, don't let one slow host stall the run

SALARY_PERIODS_PER_YEAR = {"yearly": 1, "monthly": 12, "weekly": 52, "daily": 260, "hourly": 2080}
# Anything lower is junk, not a real annual salary (Adzuna returns e.g. min=0, max=5).
MIN_PLAUSIBLE_ANNUAL_SALARY = 1000
ADZUNA_CURRENCY_BY_COUNTRY = {"gb": "GBP", "es": "EUR", "de": "EUR", "fr": "EUR", "nl": "EUR", "us": "USD", "ca": "CAD"}

# Companies discovered mid-run: fetch_full_description resolved an Adzuna
# redirect to a Greenhouse/Lever slug we don't already track in
# companies.yaml. Module-level rather than threaded through every return
# value because fetch_adzuna's return shape (list[dict] of jobs) is a
# shared contract with fetch_remotive/FETCHERS/fetch_aggregator — see
# discover_companies.py for what reads this after a run. Each process run
# starts with an empty list, so there's no cross-run leakage to worry about.
DISCOVERED_COMPANIES: list[dict] = []

_GREENHOUSE_URL_RE = re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([^/]+)/jobs/(\d+)")
_LEVER_URL_RE = re.compile(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")

# NOTE: redirect_url is ALWAYS an adzuna.* URL (their own tracking/details
# link) whether or not it goes on to redirect somewhere else — so there's
# no way to tell "self-hosted, terminal" apart from "tracking link that
# 302s to a real ATS" by looking at redirect_url's domain alone. An
# earlier version of this file tried exactly that (skip anything on
# adzuna.*) and it was wrong: it would've skipped the Greenhouse/Lever
# redirects too, since those also start as adzuna.* links before
# following through. The only way to tell them apart is to actually
# attempt the request and see what comes back — see fetch_full_description
# below (and its KNOWN LIMITATION note for the case where it stays on
# adzuna.* and 403s).


def _collapse_whitespace(text: str) -> str:
    """Scraped page text turns every stripped block-level tag into its own
    line, so a page with a search widget/sidebar/nav (see fetch_adzuna's
    Adzuna-details-page fallback) comes back as mostly blank lines around a
    handful of real content lines. Strip each line and collapse RUNS of
    blank lines down to one — not zero, since a single blank line is often
    a legitimate paragraph break (e.g. Ashby's descriptionPlain) that's
    worth keeping, not layout noise to remove entirely."""
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def _html_page_to_text(html: str) -> str:
    """Rough main-content extraction for a page we don't have a structured
    API for: drop script/style blocks (JS/CSS text isn't content), strip
    remaining tags, collapse whitespace. Will include some nav/footer
    boilerplate on sites without a recognized ATS — still far more useful
    to the AI evaluation step than a one-sentence aggregator snippet."""
    html = _SCRIPT_STYLE_RE.sub(" ", html or "")
    return _collapse_whitespace(filters.strip_html(html).strip())


def fetch_via_browser(url: str, timeout_ms: int = 20000) -> str | None:
    """Last-resort fallback for pages a plain httpx GET can't get real
    content from. UPDATED DIAGNOSIS (2026-08-12): the earlier theory here
    was a Cloudflare bot-challenge (based on this sandbox's WebFetch
    getting 403 on adzuna.ca) — but a direct httpx print from
    fetch_full_description showed a plain 200 with just an empty PAGE
    SKELETON, not a 403 or a challenge page. adzuna.ca/details/ pages are
    a client-rendered SPA: the initial HTML response is a near-empty shell
    and the actual job content gets injected by JavaScript after load —
    httpx never executes JS, so it can NEVER see that content no matter
    what headers/User-Agent it sends. This is why wait_until="networkidle"
    matters below (waits for the page's own JS/API calls to finish
    populating content) rather than "domcontentloaded" (fires as soon as
    the empty shell HTML is parsed, before any of that has happened) — an
    earlier version of this function used domcontentloaded and would have
    captured the same empty skeleton a plain httpx GET already sees,
    making the whole fallback pointless. (WebFetch's 403 might still be a
    separate, genuine bot-block layered on top for that specific tool/UA —
    unconfirmed either way; the SPA-skeleton issue is the one a real
    browser actually needs to solve, and does, by executing the JS.)

    Only called from fetch_full_description as a fallback when the plain
    request path found nothing useful — and that's only ever reached from
    fetch_adzuna's already-gated call site (technical title, snippet
    looks truncated), so this doesn't fire for the bulk of raw results,
    only the smallish set that already look like real candidates. Still
    meaningfully slower than an httpx call (browser launch + full page
    render), so keep that gating in place upstream — don't call this
    unguarded.

    Requires the optional `playwright` dependency (`pip install playwright
    && playwright install chromium` — see requirements.txt). Returns None
    if playwright isn't installed, the page errors, or the page loads but
    still has nothing to show for it (e.g. network never goes idle within
    the timeout, or there really is a bot-challenge underneath the SPA
    shell) — same "fail quietly, caller falls back to the short snippet"
    contract as the rest of this module. NOT verified against the real
    adzuna.ca from this sandbox (no network access here to test with) —
    try it for real and report back what you see."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                # networkidle, not domcontentloaded: the empty SPA shell
                # parses (and "DOM content loaded") almost instantly — the
                # real job content only shows up after the page's own JS
                # finishes fetching and rendering it, which is what
                # networkidle actually waits for.
                page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                body_text = page.inner_text("body")
            finally:
                browser.close()
    except Exception:
        return None

    return _collapse_whitespace(body_text) or None


def fetch_full_description(redirect_url: str) -> dict:
    """Best-effort: follow an aggregator's redirect_url to the real posting
    and pull the full JD text. Adzuna's search API only ever returns a
    short teaser snippet — there's no "give me the full text" parameter —
    so this is the only way to get the real description.

    KNOWN LIMITATION, PARTIALLY MITIGATED (2026-08-12): when Adzuna is the
    terminal host for a job (no external ATS — the request lands on their
    own adzuna.ca/details/... page and stays there), a plain httpx request
    gets 403'd — confirmed with two real listings, and their API doesn't
    expose a full-description field anywhere either (checked their docs).
    As of 2026-08-12 this now falls back to a real headless browser (see
    fetch_via_browser) for that case, which has a real shot at clearing
    Cloudflare's bot-challenge where httpx has none — but isn't guaranteed
    (Cloudflare fingerprints headless browsers too) and hasn't been
    verified live from this sandbox (no network access here to test with).
    If it still comes back empty, jobs fall through to the short snippet
    unchanged, and ai_evaluate.py's looks_truncated() note is what keeps
    that from tanking match_score. This DOES still recover the full JD via
    the fast path for listings whose redirect_url is a tracking link that
    goes on to a recognized ATS (Greenhouse/Lever confirmed working; see
    below) — there's no reliable way to tell the two cases apart in
    advance, so every job still gets a real attempt at the fast path first.

    Returns {"description": str|None, "ats": str|None, "slug": str|None}.
    `description` is None only if EVERY path failed (plain request, known-
    ATS API, generic scrape, AND the browser fallback); callers must still
    fall back to the short snippet rather than let one bad job kill the
    run. `ats`/`slug` are set ONLY when the redirect resolved to a
    recognized ATS's own single-job API call that actually succeeded —
    that's the signal discover_companies.py uses to suggest adding the
    company to companies.yaml, since the slug is verified by a real
    successful request, not guessed from a URL (see companies.yaml's
    Coveo/Treewalk history for why that distinction matters — a slug
    parsed off a search-result URL burned real API calls on 404s twice
    before being caught).

    Prefers the source ATS's own JSON API (single-job endpoint — cheap,
    structured, matches what ats_clients.py already parses) when the
    redirect lands on a recognized board; falls back to scraping whatever
    HTML the final page returns; falls back further to a real browser if
    that HTML was empty/useless or the initial request failed outright."""
    empty = {"description": None, "ats": None, "slug": None}
    if not redirect_url:
        return empty

    resp = None
    try:
        resp = httpx.get(
            redirect_url, headers={"User-Agent": USER_AGENT},
            timeout=FULL_JD_TIMEOUT, follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception:
        resp = None  # plain request failed outright (403/timeout/DNS/etc) -> try the browser fallback below

    if resp is not None:
        final_url = str(resp.url)

        m = _GREENHOUSE_URL_RE.search(final_url)
        if m:
            slug, job_id = m.groups()
            try:
                job_resp = httpx.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}",
                    params={"content": "true"}, headers={"User-Agent": USER_AGENT}, timeout=FULL_JD_TIMEOUT,
                )
                job_resp.raise_for_status()
                content = job_resp.json().get("content")
                if content:
                    return {"description": content, "ats": "greenhouse", "slug": slug}
            except Exception:
                pass  # fall through to generic HTML scrape / browser fallback below

        m = _LEVER_URL_RE.search(final_url)
        if m:
            slug, posting_id = m.groups()
            try:
                job_resp = httpx.get(
                    f"https://api.lever.co/v0/postings/{slug}/{posting_id}",
                    params={"mode": "json"}, headers={"User-Agent": USER_AGENT}, timeout=FULL_JD_TIMEOUT,
                )
                job_resp.raise_for_status()
                data = job_resp.json()
                content = data.get("descriptionPlain") or data.get("description")
                if content:
                    return {"description": content, "ats": "lever", "slug": slug}
            except Exception:
                pass

        text = _html_page_to_text(resp.text)
        if text:
            return {"description": text, "ats": None, "slug": None}

    # Either the plain request failed outright, or it succeeded but there
    # was nothing usable in the response (e.g. a bot-challenge shell page
    # with no real content) — last resort, try a real browser.
    browser_text = fetch_via_browser(redirect_url)
    return {"description": browser_text, "ats": None, "slug": None}


def _to_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def annual_salary(min_amount: object, max_amount: object, currency: object, period: object = "yearly") -> dict:
    """Normalizes a source's salary fields to annual salary_min/salary_max/
    salary_currency. Returns {} when there's no usable salary, so callers can
    just `**` it into the job dict."""
    periods_per_year = SALARY_PERIODS_PER_YEAR.get(str(period or "yearly").lower())
    if periods_per_year is None:
        return {}

    def to_annual(amount: object) -> float | None:
        number = _to_number(amount)
        if number is None:
            return None
        annual = number * periods_per_year
        return annual if annual >= MIN_PLAUSIBLE_ANNUAL_SALARY else None

    salary_min, salary_max = to_annual(min_amount), to_annual(max_amount)
    if salary_min is None and salary_max is None:
        return {}
    return {
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": currency.strip().upper() if isinstance(currency, str) and currency.strip() else None,
    }


def fetch_adzuna(params: dict) -> list[dict]:
    """Adzuna paginates — one page is only `results_per_page` (default 50)
    results, and the free tier's ranking means good matches can be a few
    pages deep. Fetches up to `max_pages` (default 5, set per-aggregator in
    aggregators.yaml), stopping early if a page comes back short (signals
    we've hit the end of Adzuna's result set for that query)."""
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise RuntimeError(
            "ADZUNA_APP_ID / ADZUNA_APP_KEY env vars not set — "
            "register for free at https://developer.adzuna.com"
        )

    params = dict(params)  # don't mutate the caller's dict (reused across runs)
    max_pages = params.pop("max_pages", 5)
    results_per_page = params.get("results_per_page", 50)
    country = params.pop("country", "gb")  # Adzuna country-code endpoint, e.g. "gb", "es", "de"

    jobs = []
    for page in range(1, max_pages + 1):
        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
        query = {
            "app_id": app_id,
            "app_key": app_key,
            "content-type": "application/json",
            **params,
        }
        resp = httpx.get(url, params=query, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            break

        for j in results:
            loc = (j.get("location") or {}).get("display_name", "")
            title = j.get("title", "")
            redirect_url = j.get("redirect_url", "")
            snippet = j.get("description", "")  # Adzuna gives a short snippet, not the full JD

            description = snippet
            company_name = (j.get("company") or {}).get("display_name", "Unknown")
            # Only worth the extra request for technical-looking titles whose
            # snippet is actually thin, so a run doesn't make an extra HTTP
            # request for every raw Adzuna result.
            if filters.title_looks_technical(title) and filters.looks_truncated(snippet):
                full = fetch_full_description(redirect_url)
                if full["description"] and len(full["description"]) > len(snippet):
                    description = full["description"]
                if full["ats"] and full["slug"]:
                    DISCOVERED_COMPANIES.append({
                        "name": company_name, "ats": full["ats"], "slug": full["slug"],
                    })

            # Adzuna's structured `location` is a geographic field (e.g.
            # "UK", "London, UK") — it doesn't reflect remote status even
            # when the posting clearly is remote (that only shows up in the
            # title/description text, which is exactly why `where: "remote"`
            # in aggregators.yaml returns 0 results: Adzuna's geocoder
            # doesn't recognize "remote" as a place). Confirmed live
            # 2026-09-22. So: normalize the same way as
            # fetch_remoteok/fetch_jobicy/fetch_weworkremotely — if "remote"
            # shows up in the title or description text, fold it into the
            # location string so the board shows it at a glance.
            normalized_loc = loc
            if "remote" not in loc.lower() and "remote" in (title + " " + description).lower():
                normalized_loc = f"Remote - {loc}" if loc else "Remote"

            jobs.append({
                "company": company_name,
                "title": title,
                "location": normalized_loc,
                "url": redirect_url,
                "posted_at": j.get("created"),
                "description": description,
                # salary_is_predicted == "1" is Adzuna's own estimate, not the employer's figure
                **({} if j.get("salary_is_predicted") == "1" else annual_salary(
                    j.get("salary_min"), j.get("salary_max"), ADZUNA_CURRENCY_BY_COUNTRY.get(country))),
            })

        if len(results) < results_per_page:
            break  # short page -> no more results, stop paginating early
    return jobs


def fetch_remotive(params: dict) -> list[dict]:
    url = "https://remotive.com/api/remote-jobs"
    resp = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "company": j.get("company_name", "Unknown"),
            "title": j.get("title", ""),
            "location": j.get("candidate_required_location", ""),
            "url": j.get("url", ""),
            "posted_at": j.get("publication_date"),
            "description": j.get("description", ""),  # full HTML per Remotive's docs
        })
    return jobs


def fetch_remoteok(params: dict) -> list[dict]:
    """https://remoteok.com/api — no auth, no real query params (tag/location
    query params were tested live and don't actually filter server-side), so
    this always returns the latest ~100 postings and leaves title/stack
    filtering to filters.py. Every RemoteOK listing is remote by
    construction, but the `location` field is often blank or just a hiring
    org's home city — normalized to always carry the word "remote"."""
    url = "https://remoteok.com/api"
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for j in data:
        if "position" not in j:
            continue  # first element is RemoteOK's API-terms legal notice, not a job
        raw_location = (j.get("location") or "").strip()
        jobs.append({
            "company": j.get("company", "Unknown"),
            "title": j.get("position", ""),
            "location": f"Remote - {raw_location}" if raw_location else "Remote",
            "url": j.get("url") or j.get("apply_url", ""),
            "posted_at": j.get("date"),
            "description": j.get("description", ""),
            **annual_salary(j.get("salary_min"), j.get("salary_max"), "USD"),  # RemoteOK salaries are USD/year
        })
    return jobs


def fetch_jobicy(params: dict) -> list[dict]:
    """https://jobicy.com/api/v2/remote-jobs — no auth. Supports real
    server-side filters: `count`, `tag` (loose keyword match on title, e.g.
    "dev"), `geo` (e.g. "europe", "usa"), `industry`. All Jobicy listings
    are remote by construction; `jobGeo` is a hiring-eligibility region
    (e.g. "Europe", "USA"), not itself "remote" text, so it's folded into
    the normalized location the same way as fetch_remoteok."""
    url = "https://jobicy.com/api/v2/remote-jobs"
    resp = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for j in data.get("jobs", []):
        geo = (j.get("jobGeo") or "").strip()
        jobs.append({
            "company": j.get("companyName", "Unknown"),
            "title": j.get("jobTitle", ""),
            "location": f"Remote - {geo}" if geo else "Remote",
            "url": j.get("url", ""),
            "posted_at": j.get("pubDate"),
            "description": j.get("jobDescription", ""),  # full HTML per Jobicy's docs
            **annual_salary(j.get("salaryMin"), j.get("salaryMax"), j.get("salaryCurrency"), j.get("salaryPeriod")),
        })
    return jobs


def fetch_weworkremotely(params: dict) -> list[dict]:
    """https://weworkremotely.com/categories/<category>.rss — no auth, RSS
    (not JSON). Confirmed live: the general "remote-programming-jobs"
    category mixes Full-Stack/Back-End/Front-End-tagged postings (a
    dedicated front-end-only feed URL was tried and returned 0 items, so
    don't rely on one existing) — leaves title/stack filtering to
    filters.py, same as fetch_remoteok. Every listing is remote by
    construction; `region` (e.g. "USA Only", "Anywhere in the World") is an
    eligibility hint, not itself "remote" text, so it's folded into the
    normalized location the same way as fetch_remoteok/fetch_jobicy.
    Title is "Company: Job Title" — split on the first ": ". `pubDate` is
    RFC 822, normalized to ISO 8601 here to match every other source."""
    category = params.get("category", "remote-programming-jobs")
    url = f"https://weworkremotely.com/categories/{category}.rss"
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    jobs = []
    for item in root.findall(".//item"):
        raw_title = (item.findtext("title") or "").strip()
        company, sep, title = raw_title.partition(": ")
        if not sep:
            company, title = "Unknown", raw_title
        region = (item.findtext("region") or "").strip()
        raw_pub_date = item.findtext("pubDate")
        try:
            posted_at = parsedate_to_datetime(raw_pub_date).isoformat() if raw_pub_date else None
        except (TypeError, ValueError):
            posted_at = raw_pub_date
        jobs.append({
            "company": company,
            "title": title,
            "location": f"Remote - {region}" if region else "Remote",
            "url": item.findtext("link") or item.findtext("guid") or "",
            "posted_at": posted_at,
            "description": item.findtext("description") or "",
        })
    return jobs


def _jobspy_text(value: object) -> str:
    # pandas fills missing cells with NaN (a float), not None
    return value.strip() if isinstance(value, str) else ""


def fetch_jobspy(params: dict) -> list[dict]:
    """LinkedIn/Indeed/etc via python-jobspy, which scrapes each site's
    public (logged-out) job search — no account involved. `params` go
    straight to jobspy.scrape_jobs. fetch_description defaults to True
    because LinkedIn returns no JD text otherwise, which would leave
    ai_filter.py and ai_evaluate.py with nothing to judge.
    Remote rows get "Remote" folded into location, same as fetch_remoteok."""
    from jobspy import scrape_jobs  # lazy: heavy import (pandas), only needed when a jobspy entry is configured

    df = scrape_jobs(**{"fetch_description": True, "enforce_annual_salary": True, "verbose": 0, **params})
    errors = df.attrs.get("errors", {})
    if errors and df.empty:
        raise RuntimeError(f"jobspy returned no jobs, site errors: {errors}")
    for site, error in errors.items():
        print(f"[WARN] jobspy {site}: {error}", file=sys.stderr)

    jobs = []
    for j in df.to_dict("records"):
        location = _jobspy_text(j.get("location"))
        if j.get("is_remote") is True and "remote" not in location.lower():
            location = f"Remote - {location}" if location else "Remote"
        date_posted = j.get("date_posted")
        jobs.append({
            "company": _jobspy_text(j.get("company")) or "Unknown",
            "title": _jobspy_text(j.get("title")),
            "location": location,
            "url": _jobspy_text(j.get("job_url")),
            "posted_at": date_posted.isoformat() if isinstance(date_posted, date) else None,
            "description": _jobspy_text(j.get("description")),
            "source": _jobspy_text(j.get("site")) or "jobspy",
            "apply_url": _jobspy_text(j.get("job_url_direct")) or None,  # the employer's own apply page, when known
            **annual_salary(j.get("min_amount"), j.get("max_amount"), j.get("currency")),
        })
    return jobs


FETCHERS = {
    "adzuna": fetch_adzuna,
    "remotive": fetch_remotive,
    "remoteok": fetch_remoteok,
    "jobicy": fetch_jobicy,
    "weworkremotely": fetch_weworkremotely,
    "jobspy": fetch_jobspy,
}


def fetch_aggregator(aggregator: dict) -> list[dict]:
    fetcher = FETCHERS.get(aggregator["type"])
    if fetcher is None:
        raise ValueError(f"No fetcher for aggregator type: {aggregator['type']}")
    jobs = fetcher(aggregator.get("params", {}))
    for j in jobs:
        j.setdefault("source", aggregator["type"])  # a fetcher may set a more specific one (jobspy: the site)
    return jobs