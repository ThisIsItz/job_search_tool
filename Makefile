SHELL := /bin/bash
.DEFAULT_GOAL := run

.PHONY: run test ai-filter evaluate web apply daily install-daily uninstall-daily

# Interactive wrapper around `python -m app.main` — asks the questions
# instead of you having to remember the argparse flags.
run:
	@companies_flag=""; aggregators_flag=""; discovery_flag=""; company_flag=""; \
	read -rp "Fetch from companies.yaml? [Y/n] " ans; \
	[[ "$$ans" =~ ^[Nn] ]] && companies_flag="--skip-companies"; \
	read -rp "Fetch from aggregators.yaml? [Y/n] " ans; \
	[[ "$$ans" =~ ^[Nn] ]] && aggregators_flag="--skip-aggregators"; \
	read -rp "Auto-discover new companies from Adzuna results? [Y/n] " ans; \
	[[ "$$ans" =~ ^[Nn] ]] && discovery_flag="--skip-discovery"; \
	read -rp "Only one company slug? (blank = all) " slug; \
	[[ -n "$$slug" ]] && company_flag="--company $$slug"; \
	python -m app.main $$companies_flag $$aggregators_flag $$discovery_flag $$company_flag

# Automated test suite: pytest-style tests plus the standalone main()-style
# sanity scripts that print their own pass/fail summary.
test:
	python -m pytest app/ -q
	python -m app.tests.test_pipeline
	python -m app.tests.test_discover_companies
	python -m app.tests.test_ai_evaluate

# Interactive wrapper around `python -m app.ai_filter` (Gemini Decisions
# API, free tier) — run it before `make evaluate` so Claude only scores jobs
# that survive it.
ai-filter:
	@dry_run_flag=""; limit_flag=""; \
	read -rp "Do you want a dry run? [Y/n] " ans; \
	[[ ! "$$ans" =~ ^[Nn] ]] && dry_run_flag="--dry-run"; \
	read -rp "Do you want to set a limit? (blank = all) " limit_val; \
	[[ -n "$$limit_val" ]] && limit_flag="--limit $$limit_val"; \
	python -m app.ai_filter $$dry_run_flag $$limit_flag

# Interactive wrapper around `python -m app.ai_evaluate` — asks the questions
evaluate:
	@dry_run_flag=""; limit_flag=""; \
	read -rp "Do you want a dry run? [Y/n] " ans; \
	[[ ! "$$ans" =~ ^[Nn] ]] && dry_run_flag="--dry-run"; \
	read -rp "Do you want to set a limit? (blank = all) " limit_val; \
	[[ -n "$$limit_val" ]] && limit_flag="--limit $$limit_val"; \
	python -m app.ai_evaluate $$dry_run_flag $$limit_flag

web:
	DB_PATH=$(CURDIR)/data/seen_jobs.sqlite3 npm --prefix web run dev

# Interactive wrapper around `python -m app.generate_application` — writes a
# ready-to-paste prompt (cover letter + CV tailoring notes) for ONE job
# you've picked (paste its URL from the web board). Free
# by default (paste the output into claude.ai/ChatGPT yourself); only calls
# the paid Anthropic API if you opt into --api. Never submits anything.
apply:
	@api_flag=""; \
	read -rp "Job URL: " url; \
	read -rp "Use the paid Anthropic API instead of a paste-into-chat prompt? [y/N] " ans; \
	[[ "$$ans" =~ ^[Yy] ]] && api_flag="--api"; \
	python -m app.generate_application --url "$$url" $$api_flag

# Non-interactive fetch -> filter -> AI filter -> Telegram digest, for the
# daily schedule. If any step fails, a failure notice goes to Telegram instead.
# launchd writes its output to data/logs/daily-<date>.log; logs older than
# LOG_RETENTION_DAYS are deleted at the start of each run.
# When the Mac was asleep at the scheduled time, launchd starts the run right
# on wake, before Wi-Fi reconnects (seen live: every source failed with DNS
# errors), so it first waits up to ~5 minutes for the network.
LOG_RETENTION_DAYS := 14
NETWORK_CHECK_URL := https://api.telegram.org
NETWORK_WAIT_ATTEMPTS := 30
NETWORK_WAIT_INTERVAL_SECONDS := 10

daily:
	@date
	@find data/logs -name 'daily-*.log' -mtime +$(LOG_RETENTION_DAYS) -delete
	@for attempt in $$(seq 1 $(NETWORK_WAIT_ATTEMPTS)); do \
	  /usr/bin/curl -s -o /dev/null --max-time 5 $(NETWORK_CHECK_URL) && exit 0; \
	  echo "No network yet (attempt $$attempt/$(NETWORK_WAIT_ATTEMPTS)), waiting..."; \
	  sleep $(NETWORK_WAIT_INTERVAL_SECONDS); \
	done; \
	echo "Still no network — skipping this run (the next scheduled one will catch up)."; exit 1
	@{ .venv/bin/python -u -m app.main && .venv/bin/python -u -m app.notify_telegram; } || \
	{ .venv/bin/python -m app.notify_telegram --failure "see data/logs/daily-$$(date +%F).log"; exit 1; }

DAILY_PLIST := $(HOME)/Library/LaunchAgents/com.jobsearch.daily.plist

# Schedules `make daily` every day at 09:00 and 18:00 (edit launchd/com.jobsearch.daily.plist to change it).
install-daily:
	sed 's|__REPO__|$(CURDIR)|' launchd/com.jobsearch.daily.plist > "$(DAILY_PLIST)"
	launchctl bootout gui/$$(id -u)/com.jobsearch.daily 2>/dev/null || true
	launchctl bootstrap gui/$$(id -u) "$(DAILY_PLIST)"

uninstall-daily:
	launchctl bootout gui/$$(id -u)/com.jobsearch.daily 2>/dev/null || true
	rm -f "$(DAILY_PLIST)"
