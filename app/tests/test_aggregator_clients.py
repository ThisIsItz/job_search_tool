"""Sanity test for aggregator_clients.py's full-JD-fetch logic — no real
network calls (this dev sandbox blocks outbound httpx to arbitrary domains
anyway; see the module docstrings). Mocks httpx.get/httpx.Response to
verify: (1) fetch_full_description prefers a recognized ATS's own JSON API
when the redirect lands there (and reports the resolved ats/slug back —
that's what feeds discover_companies.py), (2) falls back to scraping raw
HTML when it doesn't recognize the destination, (3) returns an empty
result cleanly on any failure, (4) fetch_adzuna only attempts the extra
fetch for jobs that already look like real candidates with a genuinely
thin snippet — not all ~2000 raw aggregator results, and (5) a resolved
Greenhouse/Lever match gets queued into DISCOVERED_COMPANIES for
main.py/discover_companies.py to pick up. Run with:
python -m pytest app/tests/test_aggregator_clients.py
"""
from unittest.mock import patch, MagicMock

from app import aggregator_clients


def _fake_response(url, json_data=None, text=""):
    resp = MagicMock()
    resp.url = url
    resp.text = text
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_data or {})
    return resp


def test_full_description_prefers_greenhouse_api():
    redirect_resp = _fake_response("https://job-boards.greenhouse.io/warnermusicgroup/jobs/1234567", text="<html>ignored</html>")
    job_resp = _fake_response("...", json_data={"content": "<p>Full JD from Greenhouse's own API.</p>"})

    with patch("httpx.get", side_effect=[redirect_resp, job_resp]) as mock_get:
        result = aggregator_clients.fetch_full_description("https://www.adzuna.ca/details/1")
    assert result["description"] == "<p>Full JD from Greenhouse's own API.</p>"
    assert result["ats"] == "greenhouse" and result["slug"] == "warnermusicgroup"
    assert mock_get.call_count == 2  # redirect follow, then the single-job API call
    print("fetch_full_description: Greenhouse single-job API preferred, ats/slug reported — OK")


def test_full_description_prefers_lever_api():
    posting_id = "22d02404-b9c9-4b77-9448-add55d961444"
    redirect_resp = _fake_response(f"https://jobs.lever.co/altaml/{posting_id}", text="<html>ignored</html>")
    job_resp = _fake_response("...", json_data={"descriptionPlain": "Full JD from Lever's own API."})

    with patch("httpx.get", side_effect=[redirect_resp, job_resp]):
        result = aggregator_clients.fetch_full_description("https://www.adzuna.ca/details/2")
    assert result["description"] == "Full JD from Lever's own API."
    assert result["ats"] == "lever" and result["slug"] == "altaml"
    print("fetch_full_description: Lever single-job API preferred, ats/slug reported — OK")


def test_full_description_falls_back_to_html_scrape():
    html = "<html><head><style>.x{color:red}</style></head><body><h1>Software Engineer</h1><p>We need Python.</p></body></html>"
    redirect_resp = _fake_response("https://careers.example.com/job/42", text=html)

    with patch("httpx.get", side_effect=[redirect_resp]):
        result = aggregator_clients.fetch_full_description("https://www.adzuna.ca/details/3")
    assert result["description"] is not None
    assert "Software Engineer" in result["description"] and "Python" in result["description"]
    assert "color:red" not in result["description"]  # style block dropped
    assert result["ats"] is None and result["slug"] is None  # unrecognized host -> no discovery signal
    print("fetch_full_description: unrecognized site falls back to HTML scrape, no discovery — OK")


def test_full_description_returns_none_on_failure():
    with patch("httpx.get", side_effect=Exception("connection refused")):
        result = aggregator_clients.fetch_full_description("https://www.adzuna.ca/details/4")
    assert result == {"description": None, "ats": None, "slug": None}
    print("fetch_full_description: network failure -> empty result, no crash — OK")

    assert aggregator_clients.fetch_full_description("") == {"description": None, "ats": None, "slug": None}
    print("fetch_full_description: empty url -> empty result — OK")


def test_adzuna_only_fetches_full_jd_for_promising_thin_snippets():
    """Gating check: fetch_full_description should NOT be called for jobs
    with a non-technical title, or whose snippet is already long enough —
    only for the subset that's both technical-looking AND thin. Location
    doesn't gate it (ai_filter.py judges location)."""
    base_result = lambda **kw: {
        "title": "Software Engineer, Backend",
        "location": {"display_name": "Alberta, Canada"},
        "company": {"display_name": "TestCo"},
        "redirect_url": "https://www.adzuna.ca/details/x",
        "created": "2026-08-01",
        "description": "Short teaser.",
        **kw,
    }
    results = [
        base_result(),  # promising + thin -> SHOULD fetch
        base_result(title="Marketing Operations Manager"),  # fails title -> should NOT fetch
        base_result(location={"display_name": "Remote Poland"}),  # location no longer gates -> SHOULD fetch
        base_result(description="A" * 500 + " already a full-length description, well past the floor."),  # not thin -> should NOT fetch
    ]
    page_resp = _fake_response("...", json_data={"results": results})

    import os
    with patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}), \
         patch("httpx.get", return_value=page_resp), \
         patch("app.aggregator_clients.fetch_full_description",
               return_value={"description": "FULL JD TEXT, much longer than the original teaser snippet.",
                              "ats": None, "slug": None}) as mock_full:
        jobs = aggregator_clients.fetch_adzuna({"results_per_page": 50, "max_pages": 1})

    assert mock_full.call_count == 2, f"expected exactly 2 full-JD fetches, got {mock_full.call_count}"
    assert jobs[0]["description"].startswith("FULL JD TEXT")
    assert jobs[1]["description"] == "Short teaser."
    assert jobs[2]["description"].startswith("FULL JD TEXT")
    assert jobs[3]["description"].startswith("AAAA")
    print("fetch_adzuna: full-JD fetch gated to technical+thin results only — OK")


def test_adzuna_queues_discovered_companies():
    """When fetch_full_description resolves a Greenhouse/Lever match, that
    company should land in DISCOVERED_COMPANIES for discover_companies.py
    to pick up after the run — see main.py."""
    result = {
        "title": "Software Engineer, Automated Marketing",
        "location": {"display_name": "Alberta, Canada"},
        "company": {"display_name": "Warner Music Group"},
        "redirect_url": "https://www.adzuna.ca/details/5702928490",
        "created": "2026-04-17",
        "description": "Short teaser…",
    }
    page_resp = _fake_response("...", json_data={"results": [result]})

    import os
    aggregator_clients.DISCOVERED_COMPANIES.clear()
    with patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}), \
         patch("httpx.get", return_value=page_resp), \
         patch("app.aggregator_clients.fetch_full_description",
               return_value={"description": "Full JD text, much longer than the teaser snippet ever was.",
                              "ats": "greenhouse", "slug": "warnermusicgroup"}):
        aggregator_clients.fetch_adzuna({"results_per_page": 50, "max_pages": 1})

    assert aggregator_clients.DISCOVERED_COMPANIES == [
        {"name": "Warner Music Group", "ats": "greenhouse", "slug": "warnermusicgroup"}
    ]
    print("fetch_adzuna: resolved Greenhouse/Lever match queued into DISCOVERED_COMPANIES — OK")


def _jobspy_frame(rows: list[dict]):
    """Built through jobspy's own JobPost model so dtypes (NaN for missing
    cells, numpy bools) match what scrape_jobs really returns."""
    import pandas as pd
    from jobspy.model import JobPost

    posts = [JobPost(**row).model_dump() for row in rows]
    return pd.DataFrame(posts, columns=list(JobPost.model_fields))


def test_jobspy_maps_rows_and_marks_remote():
    from datetime import date

    df = _jobspy_frame([
        {"job_url": "https://www.linkedin.com/jobs/view/1", "title": "Frontend Engineer",
         "company": "Acme", "is_remote": True, "date_posted": date(2026, 11, 20),
         "description": "React and TypeScript.", "site": "linkedin",
         "job_url_direct": "https://boards.greenhouse.io/acme/jobs/1"},
        {"job_url": "https://www.linkedin.com/jobs/view/2", "title": "Full Stack Engineer",
         "is_remote": False},
    ])
    with patch("jobspy.scrape_jobs", return_value=df) as mock_scrape:
        jobs = aggregator_clients.fetch_jobspy({"site_name": ["linkedin"], "search_term": "frontend"})

    assert mock_scrape.call_args.kwargs["fetch_description"] is True
    assert jobs[0] == {
        "company": "Acme", "title": "Frontend Engineer", "location": "Remote",
        "url": "https://www.linkedin.com/jobs/view/1", "posted_at": "2026-11-20",
        "description": "React and TypeScript.", "source": "linkedin",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
    }
    assert jobs[1]["source"] == "jobspy"
    assert jobs[1]["company"] == "Unknown"
    assert jobs[1]["location"] == ""
    assert jobs[1]["description"] == ""
    assert jobs[1]["posted_at"] is None


def test_fetch_aggregator_keeps_a_fetcher_specific_source():
    fake_jobs = [{"title": "A", "source": "indeed"}, {"title": "B"}]
    with patch.dict(aggregator_clients.FETCHERS, {"jobspy": lambda params: fake_jobs}):
        jobs = aggregator_clients.fetch_aggregator({"type": "jobspy"})
    assert [j["source"] for j in jobs] == ["indeed", "jobspy"]


def test_jobspy_raises_when_every_site_fails():
    import pytest

    df = _jobspy_frame([])
    df.attrs = {"errors": {"linkedin": "429 Too Many Requests"}}
    with patch("jobspy.scrape_jobs", return_value=df), pytest.raises(RuntimeError, match="429"):
        aggregator_clients.fetch_jobspy({"site_name": ["linkedin"]})



def test_annual_salary_normalizes_periods_and_currency():
    assert aggregator_clients.annual_salary(40, 50, " usd ", "hourly") == {
        "salary_min": 40 * 2080, "salary_max": 50 * 2080, "salary_currency": "USD",
    }
    assert aggregator_clients.annual_salary("60000", None, "EUR") == {
        "salary_min": 60000.0, "salary_max": None, "salary_currency": "EUR",
    }


def test_annual_salary_drops_missing_junk_and_unknown_periods():
    assert aggregator_clients.annual_salary(None, float("nan"), "USD") == {}
    assert aggregator_clients.annual_salary(0, 5, "GBP") == {}  # Adzuna's junk "0-5"
    assert aggregator_clients.annual_salary(0, 0, "USD") == {}  # RemoteOK's "no salary"
    assert aggregator_clients.annual_salary(50000, 60000, "EUR", "per-project") == {}


def test_adzuna_salary_skips_predicted_and_sets_country_currency():
    import os

    def result(**kw):
        return {"title": "Marketing Manager", "location": {"display_name": "London"},
                "company": {"display_name": "Co"}, "redirect_url": "https://www.adzuna.co.uk/x",
                "description": "A" * 500, **kw}

    results = [
        result(salary_min=70000, salary_max=100000, salary_is_predicted="0"),
        result(salary_min=53963, salary_max=53963, salary_is_predicted="1"),
    ]
    page_resp = _fake_response("...", json_data={"results": results})
    with patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}), \
         patch("httpx.get", return_value=page_resp):
        jobs = aggregator_clients.fetch_adzuna({"country": "gb", "results_per_page": 50, "max_pages": 1})

    assert (jobs[0]["salary_min"], jobs[0]["salary_max"], jobs[0]["salary_currency"]) == (70000, 100000, "GBP")
    assert "salary_min" not in jobs[1]


def test_jobspy_reads_annualised_salary():
    df = _jobspy_frame([{"job_url": "https://www.indeed.com/1", "title": "Frontend Engineer",
                         "min_amount": 45000.0, "max_amount": 55000.0, "currency": "EUR"}])
    with patch("jobspy.scrape_jobs", return_value=df) as mock_scrape:
        jobs = aggregator_clients.fetch_jobspy({"site_name": ["indeed"]})
    assert mock_scrape.call_args.kwargs["enforce_annual_salary"] is True
    assert (jobs[0]["salary_min"], jobs[0]["salary_max"], jobs[0]["salary_currency"]) == (45000.0, 55000.0, "EUR")

if __name__ == "__main__":
    test_full_description_prefers_greenhouse_api()
    test_full_description_prefers_lever_api()
    test_full_description_falls_back_to_html_scrape()
    test_full_description_returns_none_on_failure()
    test_adzuna_only_fetches_full_jd_for_promising_thin_snippets()
    test_adzuna_queues_discovered_companies()
    print("\nAll assertions passed.")