from app import duplicates

DESCRIPTION = "<p>Build observability features in React and TypeScript for a global team.</p>" * 20


def _job(url: str, **overrides) -> dict:
    return {"url": url, "company": "Elastic", "title": "Senior Full Stack Developer",
            "description": DESCRIPTION, **overrides}


def test_same_posting_in_several_countries_is_one_group():
    groups = duplicates.assign_groups([
        _job("https://e.co/spain"), _job("https://e.co/greece", company="ELASTIC"),
        _job("https://e.co/portugal", title="Senior Full-Stack Developer"),
    ])
    assert set(groups.values()) == {"https://e.co/spain"}


def test_same_title_with_a_different_description_stays_separate():
    groups = duplicates.assign_groups([
        _job("https://e.co/search-team"),
        _job("https://e.co/billing-team", description="Own the billing platform in Go and Kafka. " * 40),
    ])
    assert groups == {"https://e.co/search-team": "https://e.co/search-team",
                      "https://e.co/billing-team": "https://e.co/billing-team"}


def test_nearly_identical_descriptions_are_grouped():
    tweaked = DESCRIPTION.replace("global team", "team in Spain")
    groups = duplicates.assign_groups([_job("https://e.co/a"), _job("https://e.co/b", description=tweaked)])
    assert groups["https://e.co/b"] == "https://e.co/a"


def test_missing_descriptions_are_never_grouped():
    groups = duplicates.assign_groups([_job("https://e.co/a", description=""), _job("https://e.co/b", description="")])
    assert groups["https://e.co/b"] == "https://e.co/b"


def test_different_companies_are_never_grouped():
    groups = duplicates.assign_groups([_job("https://a.co/1"), _job("https://b.co/1", company="Grafana Labs")])
    assert groups["https://b.co/1"] == "https://b.co/1"
