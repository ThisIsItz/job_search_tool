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
        "🏢 Acme &amp; Co",
        "📍 Remote - Spain",  # work mode already in the location, not repeated
        "🎯 ●●●○ 3.5",
        "💰 €60K–80K",
        "🧩 Staff plus · Frontend · via LinkedIn",
    ]


def test_format_job_adds_work_mode_missing_from_location():
    text = notify_telegram.format_job({**JOB, "work_mode": "hybrid", "location": "Bangkok, Thailand"})
    assert "📍 Hybrid · Bangkok, Thailand" in text.splitlines()


def test_format_job_skips_missing_details():
    job = {**JOB, "stack_fit": None, "salary_min": None, "salary_max": None, "location": "",
           "work_mode": None, "seniority": None, "role_type": None, "source": "greenhouse"}
    assert notify_telegram.format_job(job).splitlines()[2:] == ["🧩 via Greenhouse"]


def test_salary_is_abbreviated():
    salary = lambda low, high, currency: notify_telegram.format_salary(
        {"salary_min": low, "salary_max": high, "salary_currency": currency})
    assert salary(70000, 95000, "EUR") == "€70K–95K"
    assert salary(145600, 145600, "USD") == "$146K"
    assert salary(None, 1_200_000, "THB") == "1.2M THB"
    assert salary(None, None, "EUR") is None


def test_stack_fit_bar_rounds_to_four_slots():
    assert notify_telegram.stack_fit_bar(3.8) == "●●●●"
    assert notify_telegram.stack_fit_bar(2.4) == "●●○○"
    assert notify_telegram.stack_fit_bar(0.2) == "○○○○"


def test_buttons_add_apply_only_for_a_distinct_direct_link():
    assert notify_telegram.build_buttons(JOB) == {
        "inline_keyboard": [[{"text": "View job", "url": "https://example.com/jobs/1"}]]}
    with_direct = notify_telegram.build_buttons({**JOB, "apply_url": "https://acme.com/apply/1"})
    assert [b["text"] for b in with_direct["inline_keyboard"][0]] == ["View job", "✅ Apply"]
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


def test_telegram_call_retries_a_dropped_connection():
    import httpx

    ok = httpx.Response(200, json={"ok": True, "result": {}}, request=httpx.Request("POST", "https://x"))
    with patch("app.notify_telegram.httpx.post", side_effect=[httpx.ConnectError("SSL EOF"), ok]) as mock_post:
        assert notify_telegram.call_telegram("token", "sendMessage", chat_id="chat", text="hi")["ok"]
    assert mock_post.call_count == 2


def test_telegram_call_gives_up_after_max_attempts():
    import httpx

    with patch("app.notify_telegram.httpx.post", side_effect=httpx.ConnectError("SSL EOF")) as mock_post, \
         pytest.raises(RuntimeError, match="SSL EOF"):
        notify_telegram.call_telegram("token", "sendMessage", chat_id="chat", text="hi")
    assert mock_post.call_count == notify_telegram.MAX_ATTEMPTS


def test_errors_never_include_the_bot_token():
    import httpx

    token = "123456:SECRET-TOKEN"
    request = httpx.Request("POST", f"https://api.telegram.org/bot{token}/sendMessage")
    unauthorized = httpx.Response(401, json={"ok": False}, request=request)
    with patch("app.notify_telegram.httpx.post", return_value=unauthorized), \
         pytest.raises(RuntimeError) as error:
        notify_telegram.call_telegram(token, "sendMessage", chat_id="chat", text="hi")
    assert token not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__
