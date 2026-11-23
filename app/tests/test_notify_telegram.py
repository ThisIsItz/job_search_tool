"""Tests for notify_telegram.py — no real Telegram calls."""
from unittest.mock import patch

import pytest

from app import dedup
from app import notify_telegram

JOB = {
    "url": "https://example.com/jobs/1", "company": "Acme & Co", "title": "Senior <Frontend> Engineer",
    "location": "Remote - Spain", "source": "linkedin", "stack_fit": 3.46,
    "salary_min": 60000.0, "salary_max": 80000.0, "salary_currency": "EUR",
}


def test_format_job_escapes_html_and_shows_details():
    text = notify_telegram.format_job(JOB)
    assert "<b>Senior &lt;Frontend&gt; Engineer</b> — Acme &amp; Co" in text
    assert "Stack fit 3.5/4 · EUR 60,000–80,000 · LinkedIn" in text
    assert text.endswith("https://example.com/jobs/1")


def test_format_job_without_optional_details():
    job = {**JOB, "stack_fit": None, "salary_min": None, "salary_max": None, "source": "greenhouse"}
    assert "\nGreenhouse\n" in notify_telegram.format_job(job)


def test_long_digests_split_without_cutting_a_job():
    jobs = [{**JOB, "url": f"https://example.com/jobs/{i}"} for i in range(60)]
    messages = notify_telegram.build_messages(jobs)
    assert len(messages) > 1
    assert all(len(m) <= notify_telegram.MAX_MESSAGE_CHARS for m in messages)
    assert messages[0].startswith("🔎 60 new matching jobs")
    assert sum(m.count("https://example.com/jobs/") for m in messages) == 60


def _save_screened(conn, url: str, passed: bool) -> None:
    dedup.save_details(conn, {**JOB, "url": url}, passed_filters=True)
    dedup.save_ai_filter(conn, url, None if passed else "role type (backend, senior)",
                         '{"stack_fit": {"score": 3.0}}', "gemini-decision-1")


def test_sends_each_match_once_and_skips_rejected(tmp_path):
    with dedup.connect(str(tmp_path / "test.sqlite3")) as conn:
        _save_screened(conn, "https://example.com/match", passed=True)
        _save_screened(conn, "https://example.com/rejected", passed=False)

        with patch("app.notify_telegram.send_message") as mock_send:
            assert notify_telegram.send_new_matches(conn, "token", "chat") == 1
            assert "https://example.com/match" in mock_send.call_args.args[2]
            assert "rejected" not in mock_send.call_args.args[2]

            assert notify_telegram.send_new_matches(conn, "token", "chat") == 0
            assert mock_send.call_count == 1


def test_failed_send_is_retried_next_run(tmp_path):
    with dedup.connect(str(tmp_path / "test.sqlite3")) as conn:
        _save_screened(conn, "https://example.com/match", passed=True)
        with patch("app.notify_telegram.send_message", side_effect=RuntimeError("network down")), \
             pytest.raises(RuntimeError):
            notify_telegram.send_new_matches(conn, "token", "chat")
        assert len(dedup.get_unnotified_matches(conn)) == 1
