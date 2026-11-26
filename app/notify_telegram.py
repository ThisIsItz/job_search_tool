"""Sends new matching jobs to you on Telegram: one message per job, with
its details and buttons to open the posting (and the employer's apply page,
when known).

"Matching" = passed filters.py AND ai_filter.py. Each run sends at most
MAX_JOBS_PER_RUN: jobs found since the last run first, then older unsent
ones to fill the remaining slots, each group best stack fit first. Unsent
jobs expire after MAX_PENDING_DAYS (they stay on the web board). Only the
first message of a run makes a sound. Runs at the end of `make daily`.

Setup:
    1. In Telegram, talk to @BotFather, send /newbot, copy the token.
    2. Send any message to your new bot.
    3. TELEGRAM_BOT_TOKEN=... and TELEGRAM_CHAT_ID=... in .env
       (python -m app.notify_telegram --find-chat-id prints the chat id).

Usage:
    python -m app.notify_telegram                 # send new matches, if any
    python -m app.notify_telegram --failure "..." # tell you the daily run failed
    python -m app.notify_telegram --find-chat-id  # print the chat id of whoever messaged the bot
"""
import argparse
import html
import os
import sys
import time

import httpx
from dotenv import load_dotenv

from app import dedup

load_dotenv()

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 20.0
MAX_JOBS_PER_RUN = 10
MAX_PENDING_DAYS = 7
# Telegram asks bots to stay around one message per second per chat.
SECONDS_BETWEEN_MESSAGES = 1.0
# A job posted in many countries lists only this many of the others, then "+N more".
MAX_OTHER_LOCATIONS_SHOWN = 5
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 3.0
SOURCE_LABEL = {"linkedin": "LinkedIn", "indeed": "Indeed", "remoteok": "RemoteOK", "weworkremotely": "WeWorkRemotely"}


CURRENCY_SYMBOL = {"EUR": "€", "GBP": "£", "USD": "$"}
STACK_FIT_MAX = 4


def _short_amount(amount: float) -> str:
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M"
    return f"{amount / 1000:.0f}K"


def format_salary(job: dict) -> str | None:
    low, high, currency = job.get("salary_min"), job.get("salary_max"), job.get("salary_currency") or ""
    if low is None and high is None:
        return None
    symbol = CURRENCY_SYMBOL.get(currency)
    prefix, suffix = (symbol, "") if symbol else ("", f" {currency}".rstrip())
    if low is None or high is None or low == high:
        return f"{prefix}{_short_amount(low or high)}{suffix}"
    return f"{prefix}{_short_amount(low)}–{_short_amount(high)}{suffix}"


def stack_fit_bar(score: float) -> str:
    filled = max(0, min(STACK_FIT_MAX, round(score)))
    return "●" * filled + "○" * (STACK_FIT_MAX - filled)


def _label(value: str | None) -> str | None:
    return value.replace("_", " ").capitalize() if value else None


def format_job(job: dict) -> str:
    location = job.get("location") or ""
    work_mode = _label(job.get("work_mode"))
    if work_mode and work_mode.lower() not in location.lower():
        location = f"{work_mode} · {location}" if location else work_mode
    source = job.get("source") or ""
    source_label = SOURCE_LABEL.get(source, source.capitalize())
    kind = " · ".join(filter(None, [_label(job.get("seniority")), _label(job.get("role_type")),
                                     f"via {source_label}" if source_label else None]))
    stack_fit = job.get("stack_fit")
    salary = format_salary(job)
    lines = [
        f"<b>{html.escape(job['title'])}</b>",
        f"🏢 {html.escape(job['company'])}",
        f"📍 {html.escape(location)}" if location else None,
        f"🌍 Also in: {html.escape(format_other_locations(job.get('other_locations') or []))}"
        if job.get("other_locations") else None,
        f"🎯 {stack_fit_bar(stack_fit)} {stack_fit:.1f}" if stack_fit is not None else None,
        f"💰 {html.escape(salary)}" if salary else None,
        f"🧩 {html.escape(kind)}" if kind else None,
    ]
    return "\n".join(line for line in lines if line)


def format_other_locations(locations: list[str]) -> str:
    unique = list(dict.fromkeys(locations))
    shown, hidden_count = unique[:MAX_OTHER_LOCATIONS_SHOWN], len(unique) - MAX_OTHER_LOCATIONS_SHOWN
    return ", ".join(shown) + (f" +{hidden_count} more" if hidden_count > 0 else "")


def group_queue(queue: list[dict]) -> list[list[dict]]:
    """Groups the same job posted in several countries (see app/duplicates.py),
    keeping queue order: each group sits where its best member was, and that
    member comes first."""
    groups: dict[str, list[dict]] = {}
    for job in queue:
        groups.setdefault(job["group_id"], []).append(job)
    return list(groups.values())


def build_buttons(job: dict) -> dict:
    buttons = [{"text": "View job", "url": job["url"]}]
    if job.get("apply_url") and job["apply_url"] != job["url"]:
        buttons.append({"text": "✅ Apply", "url": job["apply_url"]})
    return {"inline_keyboard": [buttons]}


def _post_with_retries(url: str, params: dict) -> httpx.Response:
    # Connections to Telegram occasionally drop mid-handshake (seen live: SSL UNEXPECTED_EOF).
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return httpx.post(url, json=params, timeout=TIMEOUT)
        except httpx.TransportError:
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY_SECONDS * attempt)


def call_telegram(token: str, method: str, **params) -> dict:
    try:
        resp = _post_with_retries(TELEGRAM_API.format(token=token, method=method), params)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        # httpx error messages include the request URL, which contains the bot token;
        # `from None` keeps the original (URL-bearing) exception out of tracebacks and logs.
        raise RuntimeError(f"Telegram {method} failed: {str(e).replace(token, '<token>')}") from None
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
    return data


def send_message(token: str, chat_id: str, text: str, buttons: dict | None = None, silent: bool = False) -> None:
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True, "disable_notification": silent}
    if buttons:
        params["reply_markup"] = buttons
    call_telegram(token, "sendMessage", **params)


def find_chat_ids(token: str) -> set[int]:
    updates = call_telegram(token, "getUpdates")["result"]
    return {u["message"]["chat"]["id"] for u in updates if "message" in u}


def _mark_group_sent(conn, group: list[dict]) -> None:
    dedup.mark_notified(conn, [job["url"] for job in group])
    conn.commit()


def send_new_matches(conn, token: str, chat_id: str) -> int:
    """One message per job (a job posted in several countries is one job).
    Returns how many were sent. Each is marked sent right after its own
    message goes through, so a failure mid-run doesn't resend the ones
    already delivered. A new country version of a job you already got is
    marked sent without a message."""
    groups = group_queue(dedup.get_notification_queue(conn, MAX_PENDING_DAYS))
    for group in groups:
        if group[0]["group_already_sent"]:
            _mark_group_sent(conn, group)
    new_groups = [group for group in groups if not group[0]["group_already_sent"]]
    to_send, waiting_count = new_groups[:MAX_JOBS_PER_RUN], len(new_groups) - MAX_JOBS_PER_RUN
    for i, group in enumerate(to_send):
        if i:
            time.sleep(SECONDS_BETWEEN_MESSAGES)
        job = {**group[0], "other_locations": [other["location"] for other in group[1:] if other["location"]]}
        send_message(token, chat_id, format_job(job), build_buttons(job), silent=i > 0)
        _mark_group_sent(conn, group)
    if waiting_count > 0:
        time.sleep(SECONDS_BETWEEN_MESSAGES)
        send_message(token, chat_id, f"…and {waiting_count} more on the board. They'll come in the next "
                     f"runs if there's room (for up to {MAX_PENDING_DAYS} days).", silent=True)
    return len(to_send)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--failure", help="Send this failure notice instead of the job digest")
    parser.add_argument("--find-chat-id", action="store_true", help="Print the chat id of whoever messaged the bot")
    args = parser.parse_args()

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        sys.exit("TELEGRAM_BOT_TOKEN env var not set.")

    if args.find_chat_id:
        chat_ids = find_chat_ids(bot_token)
        if not chat_ids:
            sys.exit("No messages found — send any message to your bot first, then retry.")
        print("\n".join(str(c) for c in chat_ids))
        sys.exit(0)

    target_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not target_chat_id:
        sys.exit("TELEGRAM_CHAT_ID env var not set (python -m app.notify_telegram --find-chat-id).")

    if args.failure:
        send_message(bot_token, target_chat_id, f"⚠️ Daily job search failed: {html.escape(args.failure)}")
        sys.exit(0)

    with dedup.connect() as db:
        sent = send_new_matches(db, bot_token, target_chat_id)
    print(f"Sent {sent} new matching job(s) to Telegram.")
