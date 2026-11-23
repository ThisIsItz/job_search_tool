"""Tests for notify_telegram.py — no real Telegram calls."""
import json
from unittest.mock import patch

import pytest

from app import dedup
from app import notify_telegram

JOB = {
    "url": "https://example.com/jobs/1", "company": "Acme & Co", "title": "Senior <Frontend> Engineer",
    "location": "Remote - Spain", "source": "linkedin", "stack_fit": 3.46,
    "salary_min": 60000.0, "salary_max": 80000.0, "salary_currency": "EUR",
    "work_mode": "remote", "seniority": "staff_plus", "role_type": "frontend", "apply_url": None,
}


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("app.notify_telegram.time.sleep"):
        yield


def test_format_job_escapes_html_and_shows_details():
    text = notify_telegram.format_job(JOB)
    assert text.splitlines() == [
        "<b>Senior &lt;Frontend&gt; Engineer</b>",
        "Acme &amp; Co · Remote - Spain",
        "Stack fit 3.5/4 · EUR 60,000–80,000",
        "Remote · Staff plus · Frontend",
        "LinkedIn",
    ]


def test_format_job_skips_missing_details():
    job = {**JOB, "stack_fit": None, "salary_min": None, "salary_max": None,
           "work_mode": None, "seniority": None, "role_type": None, "source": "greenhouse"}
    assert notify_telegram.format_job(job).splitlines()[2:] == ["Greenhouse"]


def test_buttons_add_apply_only_for_a_distinct_direct_link():
    assert notify_telegram.build_buttons(JOB) == {
        "inline_keyboard": [[{"text": "View job", "url": "https://example.com/jobs/1"}]]}
    with_direct = notify_telegram.build_buttons({**JOB, "apply_url": "https://acme.com/apply/1"})
    assert [b["text"] for b in with_direct["inline_keyboard"][0]] == ["View job", "Apply"]
    same_link = notify_telegram.build_buttons({**JOB, "apply_url": JOB["url"]})
    assert [b["text"] for b in same_link["inline_keyboard"][0]] == ["View job"]


def _save_screened(conn, url: str, stack_fit: float = 3.0, passed: bool = True) -> None:
    dedup.save_details(conn, {**JOB, "url": url}, passed_filters=True)
    dedup.save_ai_filter(conn, url, None if passed else "role type (backend, senior)",
                         json.dumps({"stack_fit": {"score": stack_fit}}), "gemini-decision-1")


def _sent_urls(mock_send) -> list[str]:
    return [c.args[3]["inline_keyboard"][0][0]["url"] for c in mock_send.call_args_list if len(c.args) > 3]


def test_one_message_per_job_best_fit_first_only_first_one_rings(tmp_path):
    with dedup.connect(str(tmp_path / "test.sqlite3")) as conn:
        _save_screened(conn, "https://example.com/ok", stack_fit=2.0)
        _save_screened(conn, "https://example.com/best", stack_fit=3.8)
        _save_screened(conn, "https://example.com/rejected", passed=False)

        with patch("app.notify_telegram.send_message") as mock_send:
            assert notify_telegram.send_new_matches(conn, "token", "chat") == 2
        assert _sent_urls(mock_send) == ["https://example.com/best", "https://example.com/ok"]
        assert [c.kwargs["silent"] for c in mock_send.call_args_list] == [False, True]

        with patch("app.notify_telegram.send_message") as mock_send:
            assert notify_telegram.send_new_matches(conn, "token", "chat") == 0
        mock_send.assert_not_called()


def test_overflow_waits_and_fills_free_slots_after_new_jobs(tmp_path):
    limit = notify_telegram.MAX_JOBS_PER_RUN
    with dedup.connect(str(tmp_path / "test.sqlite3")) as conn:
        for i in range(limit + 3):
            _save_screened(conn, f"https://example.com/first-batch/{i}", stack_fit=3.9 - i * 0.1)
        with patch("app.notify_telegram.send_message") as mock_send:
            assert notify_telegram.send_new_matches(conn, "token", "chat") == limit
        assert "3 more on the board" in mock_send.call_args_list[-1].args[2]

        # Next run: a new job comes before the leftovers, even with a worse fit.
        conn.execute("UPDATE notified_jobs SET notified_at = datetime('now', '-1 hour')")
        conn.execute("UPDATE job_details SET fetched_at = datetime('now', '-2 hours')")
        _save_screened(conn, "https://example.com/new", stack_fit=1.6)
        with patch("app.notify_telegram.send_message") as mock_send:
            assert notify_telegram.send_new_matches(conn, "token", "chat") == 4
        sent = _sent_urls(mock_send)
        assert sent[0] == "https://example.com/new"
        assert len(sent) == 4 and all("first-batch" in url for url in sent[1:])


def test_unsent_jobs_expire(tmp_path):
    with dedup.connect(str(tmp_path / "test.sqlite3")) as conn:
        _save_screened(conn, "https://example.com/stale")
        conn.execute("UPDATE job_details SET fetched_at = datetime('now', ?)",
                     (f"-{notify_telegram.MAX_PENDING_DAYS + 1} days",))
        assert dedup.get_notification_queue(conn, notify_telegram.MAX_PENDING_DAYS) == []


def test_failed_send_keeps_the_job_for_next_run(tmp_path):
    with dedup.connect(str(tmp_path / "test.sqlite3")) as conn:
        _save_screened(conn, "https://example.com/match")
        with patch("app.notify_telegram.send_message", side_effect=RuntimeError("network down")), \
             pytest.raises(RuntimeError):
            notify_telegram.send_new_matches(conn, "token", "chat")
        assert len(dedup.get_notification_queue(conn, notify_telegram.MAX_PENDING_DAYS)) == 1
