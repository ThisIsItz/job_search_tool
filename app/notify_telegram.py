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
SOURCE_LABEL = {"linkedin": "LinkedIn", "indeed": "Indeed", "remoteok": "RemoteOK", "weworkremotely": "WeWorkRemotely"}


def format_salary(job: dict) -> str | None:
    low, high, currency = job.get("salary_min"), job.get("salary_max"), job.get("salary_currency") or ""
    if low is None and high is None:
        return None
    if low is None or high is None or low == high:
        return f"{currency} {(low or high):,.0f}".strip()
    return f"{currency} {low:,.0f}–{high:,.0f}".strip()


def _joined(parts: list[str | None]) -> str:
    return html.escape(" · ".join(p for p in parts if p))


def format_job(job: dict) -> str:
    source = job.get("source") or ""
    fit_and_pay = [
        f"Stack fit {job['stack_fit']:.1f}/4" if job.get("stack_fit") is not None else None,
        format_salary(job),
    ]
    kind = [(job.get(key) or "").replace("_", " ").capitalize() or None
            for key in ("work_mode", "seniority", "role_type")]
    lines = [
        f"<b>{html.escape(job['title'])}</b>",
        _joined([job["company"], job.get("location")]),
        _joined(fit_and_pay),
        _joined(kind),
        _joined([SOURCE_LABEL.get(source, source.capitalize()) or None]),
    ]
    return "\n".join(line for line in lines if line)


def build_buttons(job: dict) -> dict:
    buttons = [{"text": "View job", "url": job["url"]}]
    if job.get("apply_url") and job["apply_url"] != job["url"]:
        buttons.append({"text": "Apply", "url": job["apply_url"]})
    return {"inline_keyboard": [buttons]}


def call_telegram(token: str, method: str, **params) -> dict:
    resp = httpx.post(TELEGRAM_API.format(token=token, method=method), json=params, timeout=TIMEOUT)
    resp.raise_for_status()
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


def send_new_matches(conn, token: str, chat_id: str) -> int:
    """Returns how many jobs were sent. Each job is marked sent right after
    its own message goes through, so a failure mid-run doesn't resend the
    ones already delivered."""
    queue = dedup.get_notification_queue(conn, MAX_PENDING_DAYS)
    to_send, waiting_count = queue[:MAX_JOBS_PER_RUN], len(queue) - MAX_JOBS_PER_RUN
    for i, job in enumerate(to_send):
        if i:
            time.sleep(SECONDS_BETWEEN_MESSAGES)
        send_message(token, chat_id, format_job(job), build_buttons(job), silent=i > 0)
        dedup.mark_notified(conn, [job["url"]])
        conn.commit()
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
