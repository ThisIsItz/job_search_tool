# AI-Powered Job Search Automation


An automated pipeline for a targeted job search: fetch open roles directly
from companies' ATS APIs and job aggregators, filter out anything
irrelevant, deduplicate against everything already seen, optionally score
each candidate's fit against your own experience with Claude, and review
the results in a local web board.

Nothing here talks to any service other than the job sources you configure
and (for the optional AI steps) the Gemini and Anthropic APIs. All state — scraped
jobs, scores, your own notes — lives in a local SQLite database; nothing
is sent to a third party beyond fetching the postings themselves.

<img width="2532" height="1215" alt="image" src="https://github.com/user-attachments/assets/679cea89-3156-4e46-af3c-74d3e0d9b30c" />

## How it works

1. **Fetch** (`app/main.py`) — pulls open roles from:
   - `companies.yaml` — known companies queried directly via their ATS
     (Greenhouse, Ashby, Workable, Lever, SmartRecruiters) — precise, low
     noise.
   - `aggregators.yaml` — broad keyword+location search across many
     employers at once (Adzuna, Remotive, and LinkedIn/Indeed via
     [JobSpy](https://github.com/speedyapply/JobSpy)) — wider reach, more noise.
2. **Filter** (`app/filters.py`) — drops only what's obviously out: titles
   matching `filters.yaml`'s exclusion keywords (sales, marketing, nursing…)
   and postings older than `max_posting_age_days`. Everything else goes on
   to the AI filter.
3. **Dedup** (`app/dedup.py`) — everything fetched is stored in a local
   SQLite database (`data/seen_jobs.sqlite3`), keyed by URL and by
   normalized company+title, so re-runs only ever surface genuinely new
   postings.
4. **AI filter** (`app/ai_filter.py`, optional, free tier) — asks
   Gemini's Decisions API closed questions about each filtered job (remote
   vs hybrid vs on-site, hireable from your regions, role type, seniority,
   stack fit, required languages) and drops the clear misses, so the paid
   step below only scores what's left. Only the posting and your
   `core_stack` technologies are sent to Google.
5. **AI evaluation** (`app/ai_evaluate.py`, optional, costs money) — sends
   each filtered candidate plus your `profile.yaml` to Claude, which scores
   fit (0-100) and returns concrete gaps, transferable strengths, risk
   factors, and an apply/consider/skip recommendation.
6. **Review** (`web/`) — a local Next.js app reading/writing the same
   SQLite database directly, for browsing results and tracking your own
   `applied / interview / rejected / skipped / silence` status and notes.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in .env with the keys you need — see the comments in that file
```

Create your own config from the templates (these files are gitignored —
they hold your personal experience and search preferences):

```bash
cp profile.example.yaml profile.yaml
cp companies.example.yaml companies.yaml
cp aggregators.example.yaml aggregators.yaml
# then edit profile.yaml with your own background
```

`profile.example.yaml` is a blank template — it tells you the shape but
not the bar. For a fully worked (fictional) example showing the level of
specificity/quantification each evidence bullet should actually have, see
[`profile.sample.yaml`](profile.sample.yaml).

`ANTHROPIC_API_KEY` is only needed for the AI evaluation step. Adzuna
(`ADZUNA_APP_ID`/`ADZUNA_APP_KEY`) is only needed if you keep an Adzuna
entry in `aggregators.yaml` — register a free key at
[developer.adzuna.com](https://developer.adzuna.com). Remotive needs no
auth but only covers remote roles. JobSpy (LinkedIn/Indeed) needs no auth
either — it scrapes the sites' public, logged-out job search.

### Customize for your own search

This repo ships pre-configured for the original author's search. **Before
your first run, check these:**

1. **`profile.yaml` → `core_stack`** — the technologies the AI filter
   scores stack fit against.
2. **`profile.yaml` → `ai_filter`** — where you can be hired, an optional
   relocation city, the languages you speak, and which role
   types/seniorities you accept (see `profile.example.yaml`).
3. **`companies.yaml`** — the company registry: add/remove companies to
   match who you're actually applying to (see the file's header comment for
   the format).
4. **`aggregators.yaml`** — the keyword/location searches: update them to
   what and where you're looking.

## Usage

### 1. Fetch + filter (free)

```bash
python -m app.main                    # all companies + aggregators
python -m app.main --company affirm   # just one company, for debugging a fetcher
python -m app.main --skip-aggregators # companies.yaml only
python -m app.main --skip-companies   # aggregators.yaml only

make run                              # same thing, interactive prompts instead of flags
```

Results land in `data/seen_jobs.sqlite3`; browse them on the web board (step 4).
When `GEMINI_API_KEY` is set, this also runs the AI filter (step 2) on the
new jobs at the end — pass `--skip-ai-filter` to skip it.

### 2. AI filter (optional, free tier)

Needs `GEMINI_API_KEY` in `.env` (free at
[aistudio.google.com](https://aistudio.google.com)). Rejection rules and
your work regions/languages are constants at the top of
`app/ai_filter.py`; each verdict and its raw probabilities are stored in
the `ai_filter` table so you can audit why a job was dropped.

```bash
python -m app.ai_filter --dry-run  # see what's queued, no API calls
python -m app.ai_filter --limit 20 # screen just 20 first
python -m app.ai_filter            # screen everything not screened yet
python -m app.ai_filter --rescreen # also re-screen screened jobs, after changing the rules

make ai-filter                        # interactive prompts instead of flags
```

Rejected jobs are skipped by step 3 and hidden from the web board.

### 3. AI evaluation (optional, costs money)

```bash
python -m app.ai_evaluate --dry-run   # see what's queued, no API calls, no cost
python -m app.ai_evaluate --limit 20  # score just 20, to sanity-check quality/cost first
python -m app.ai_evaluate             # score everything unscored

make evaluate                         # interactive prompts instead of flags
```

Results are stored in SQLite (so re-running never re-pays for a job
already scored) and shown on the web board.

### 4. Review results

```bash
cd web && npm install    # first time only
cd ..
make web                              # starts the board at http://localhost:3000
```

Reads/writes `data/seen_jobs.sqlite3` directly — no export/import step.
Shows each job's source and, where the source provides one (Adzuna, Jobicy,
RemoteOK, LinkedIn/Indeed), its annual salary, with a minimum-salary filter.
See [`web/README.md`](web/README.md) for details (custom `DB_PATH`,
production build, etc).

### 5. Daily run with Telegram alerts (optional)

`make daily` runs fetch -> filter -> AI filter -> sends you one Telegram
message per job that passed, best stack fit first, with buttons to open the
posting (and the employer's apply page, when known). At most
`MAX_JOBS_PER_RUN` (10) per run: new jobs first, then older unsent ones fill
any free slots, for up to `MAX_PENDING_DAYS` (7). Only the first message of
a run makes a sound. If a step fails, you get a failure notice instead.

1. In Telegram, message @BotFather, send `/newbot`, and put the token in
   `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot, then run
   `python -m app.notify_telegram --find-chat-id` and put the number in
   `.env` as `TELEGRAM_CHAT_ID`.
3. `make install-daily` schedules it every day at 09:00 and 18:00 via launchd (logs in
   `data/logs/daily-<date>.log`, one per day, deleted after 14 days); `make uninstall-daily` removes it. It only runs while
   the Mac is on — if it's asleep at those times, it runs on wake, after
   waiting up to ~5 minutes for the network to come back.

## Makefile commands

Thin wrappers over the commands above — run from the repo root:

| Command        | Equivalent to                    | What it does |
|-----------------|----------------------------------|--------------|
| `make run`      | `python -m app.main`             | Fetch + filter (step 1). Interactively asks whether to skip companies/aggregators/discovery and whether to limit to one company slug, instead of you remembering the flags. |
| `make ai-filter` | `python -m app.ai_filter` | AI filter (step 2). Interactively asks for `--dry-run` and an optional `--limit`. |
| `make evaluate` | `python -m app.ai_evaluate`       | AI evaluation (step 3). Interactively asks for `--dry-run` and an optional `--limit`. |
| `make web`      | `npm --prefix web run dev`       | Starts the Next.js review board, with `DB_PATH` already pointed at `data/seen_jobs.sqlite3`. |
| `make test`     | `pytest app/` + the standalone sanity-check scripts | Runs the full test suite (see the Tests section below). |

`make` with no target runs `make run` (the default goal).

## Configuration

- **`companies.yaml`** — the company registry (name, ATS type, slug). Add
  a company here once you've identified its ATS.
- **`aggregators.yaml`** — aggregator search config (keywords, location,
  pagination limits).
- **`filters.yaml`** — title allowlist/exclusion keywords, location
  allowlist patterns, and JD stack-dealbreaker/core-stack keywords. Edit
  this directly as you refine what counts as in-scope for you — no code
  changes needed (matching logic lives in `app/filters.py`).
- **`profile.yaml`** — your experience profile fed to the AI evaluation
  step (see `profile.example.yaml` for the blank template and
  `profile.sample.yaml` for a fully worked example).

## Project layout

```
app/
  main.py                 orchestrates fetch -> filter -> dedup -> SQLite
  ats_clients.py           one fetch function per ATS
  aggregator_clients.py    one fetch function per aggregator
  filters.py               loads and applies filters.yaml's rules
  dedup.py                 SQLite store (seen_jobs, job_details)
  discover_companies.py    auto-appends newly-resolved companies to companies.yaml
  ai_filter.py          Gemini Decisions API screening before Claude scoring
  ai_evaluate.py           Claude-based fit scoring
  inspect_job.py           CLI to look up a stored job or list recent rejections
  refilter.py              re-runs current filters.py against already-fetched jobs
  scripts/                 one-off diagnostic/maintenance scripts, not part of the pipeline
  tests/                   pytest + standalone sanity-check scripts
web/                       Next.js review board (see web/README.md)
companies.example.yaml     template for companies.yaml (company -> ATS registry, gitignored)
aggregators.example.yaml   template for aggregators.yaml (aggregator searches, gitignored)
filters.yaml               title exclusions and posting age filter rules
profile.example.yaml       blank template for profile.yaml (your real profile, gitignored)
profile.sample.yaml        fully worked (fictional) example of a filled-in profile
```

## Tests

```bash
make test
```

Runs the pytest suite plus the standalone sanity-check scripts
(`test_pipeline.py`, `test_discover_companies.py`, `test_ai_evaluate.py`).
None of them call live external APIs.

## Notes on scope

- ATS fetchers for Greenhouse, Ashby, Workable, and Lever were each
  validated against real live responses. SmartRecruiters support is built
  from documentation and third-party corroboration only — verify a new
  SmartRecruiters company with `python -m app.main --company <slug>`
  before trusting it in a real run.
- If a particular company's fetch fails, `main.py` logs a warning and
  continues with the rest rather than crashing the whole run.

## Possible next steps

- Workday support (`clio.wd3.myworkdayjobs.com`-style boards) — its
  job-search API is POST-based with a different pagination shape than the
  ATSes currently supported.
- Company-name normalization for aggregator-sourced dedup (the same
  posting sometimes comes back under slightly different company name
  strings, e.g. "Acme Corp" vs. "Acme").
- A digest output (email/Sheet) beyond the current CSV/SQLite/web-board
  review flow.
- Scheduling: once you're happy with a full local run, a daily cron entry
  like `0 7 * * * cd /path/to/job_search_pipeline && python -m app.main && python -m app.ai_evaluate`.

## License

[MIT](LICENSE)
