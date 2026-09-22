SHELL := /bin/bash
.DEFAULT_GOAL := run

.PHONY: run test evaluate web apply

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
# you've picked (paste its URL from the web board or candidates.csv). Free
# by default (paste the output into claude.ai/ChatGPT yourself); only calls
# the paid Anthropic API if you opt into --api. Never submits anything.
apply:
	@api_flag=""; \
	read -rp "Job URL: " url; \
	read -rp "Use the paid Anthropic API instead of a paste-into-chat prompt? [y/N] " ans; \
	[[ "$$ans" =~ ^[Yy] ]] && api_flag="--api"; \
	python -m app.generate_application --url "$$url" $$api_flag
