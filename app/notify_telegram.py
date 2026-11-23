"""Sends new matching jobs to you on Telegram, one digest per run.

"Matching" = passed filters.py AND ai_filter.py. Each job is sent once
(tracked in the notified_jobs table), best stack fit first. Meant to run
after `app.main` and `app.ai_filter` — see `make daily`.

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

import httpx
from dotenv import load_dotenv

from app import dedup

load_dotenv()

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 20.0
# Telegram rejects messages over 4096 characters; leave room for the header.
MAX_MESSAGE_CHARS = 3800
SOURCE_LABEL = {"linkedin": "LinkedIn", "indeed": "Indeed", "remoteok": "RemoteOK", "weworkremotely": "WeWorkRemotely"}


def format_salary(job: dict) -> str | None:
    low, high, currency = job.get("salary_min"), job.get("salary_max"), job.get("salary_currency") or ""
    if low is None and high is None:
        return None
    if low is None or high is None or low == high:
        return f"{currency} {(low or high):,.0f}".strip()
    return f"{currency} {low:,.0f}–{high:,.0f}".strip()


def format_job(job: dict) -> str:
    source = job.get("source") or ""
    details = [
        f"Stack fit {job['stack_fit']:.1f}/4" if job.get("stack_fit") is not None else None,
        format_salary(job),
        SOURCE_LABEL.get(source, source.capitalize()) or None,
    ]
    return (
        f"<b>{html.escape(job['title'])}</b> — {html.escape(job['company'])}\n"
        f"{html.escape(job['location'] or '')}\n"
        f"{html.escape(' · '.join(d for d in details if d))}\n"
        f"{html.escape(job['url'])}"
    )


def build_messages(jobs: list[dict]) -> list[str]:
    """Splits the digest into as few messages as fit Telegram's size limit,
    never cutting a job in half."""
    header = f"🔎 {len(jobs)} new matching job{'s' if len(jobs) != 1 else ''}"
    messages, current = [], header
    for job in jobs:
        block = "\n\n" + format_job(job)
        if len(current) + len(block) > MAX_MESSAGE_CHARS:
            messages.append(current)
            current = block.lstrip()
        else:
            current += block
    messages.append(current)
    return messages


def call_telegram(token: str, method: str, **params) -> dict:
    resp = httpx.post(TELEGRAM_API.format(token=token, method=method), json=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
    return data


def send_message(token: str, chat_id: str, text: str) -> None:
    call_telegram(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                  disable_web_page_preview=True)


def find_chat_ids(token: str) -> set[int]:
    updates = call_telegram(token, "getUpdates")["result"]
    return {u["message"]["chat"]["id"] for u in updates if "message" in u}


def send_new_matches(conn, token: str, chat_id: str) -> int:
    jobs = dedup.get_unnotified_matches(conn)
    if not jobs:
        return 0
    for message in build_messages(jobs):
        send_message(token, chat_id, message)
    # Only after every message went through, so a failed send is retried next run.
    dedup.mark_notified(conn, [job["url"] for job in jobs])
    return len(jobs)


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
