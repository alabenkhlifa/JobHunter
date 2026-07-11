# JobHunter

JobHunter is an approval-gated job-search and application assistant designed to work well with [Hermes Agent](https://hermes-agent.nousresearch.com/).

It can:

- collect job opportunities from supported sources;
- store and deduplicate jobs in local SQLite;
- send at most the best 5 Telegram CTA job cards per day for review;
- learn from Interested/Skip feedback to demote repeatedly declined patterns and boost similar strong matches;
- generate truthful tailored resume / cover-letter packages from a local candidate profile;
- inspect LinkedIn / external ATS application pages through Chromium CDP;
- upload or submit only after explicit user approval;
- monitor a dedicated jobs Gmail mailbox for recruiter/ATS replies;
- sync an application tracker to a shared Google Sheet, including status, dates, resume/cover-letter links, and evidence screenshots;
- store generated ATS credentials in a local encrypted vault.

JobHunter is intentionally **not** a blind mass-apply bot. It stops on privacy notices, T&C, salary, visa/work authorization, CAPTCHA/security checks, and final submit unless the user explicitly approves.

## Quick start

```bash
git clone <your-fork-url> JobHunter
cd JobHunter
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp data/master-profile.example.json data/master-profile.json
python -m pytest -q tests
```

Edit:

- `.env` — Telegram bot token/chat ID if you want Telegram cards/buttons.
- `data/master-profile.json` — your truthful candidate profile. This file is ignored by git.

For the full setup guide, see [`setup.md`](setup.md).

## Hermes Agent setup

If you use Hermes Agent, clone the repo and start Hermes from the repo root:

```bash
cd JobHunter
hermes
```

Hermes will read [`AGENTS.md`](AGENTS.md) automatically as project instructions. You can then ask:

```text
Set up JobHunter for me using setup.md. Keep all credentials and personal data local, run the tests, and tell me what is missing.
```

Recommended Hermes toolsets for full operation:

- terminal
- file
- browser or local Chromium/CDP access
- web
- cronjob, if you want scheduled collection/review
- messaging, if using Telegram gateway integration

## Account model

JobHunter is designed around two Google identities:

| Account | Purpose |
|---|---|
| Main/personal Google account | The human owner account. It owns or can view the shared Google Sheet/Drive folder. |
| Dedicated jobs/agent Gmail account | The mailbox/API identity used by JobHunter for ATS verification emails, recruiter replies, approved outbound mail, and Google Sheets/Drive automation. |

Recommended pattern:

1. Create the tracker spreadsheet from the main/personal account.
2. Share it with the dedicated jobs Gmail account as **Editor**.
3. When JobHunter uploads resumes, cover letters, and screenshots to Drive through the jobs Gmail account, also grant the main/personal account access to the generated Drive folder/files so the human owner can open every link in the tracker.
4. Keep OAuth tokens and client secrets outside the repo.

## Core commands

Collect jobs:

```bash
python scraper.py --collect-only
```

Inspect a job:

```bash
python scraper.py --get-job <job_id>
```

Mark a job as interested:

```bash
python scraper.py --mark-interested <job_id>
```

Inspect the currently open application page through Chromium CDP:

```bash
python -m jobhunter_auto_apply.cli inspect --job-id <job_id>
```

Upload only after explicit approval:

```bash
python -m jobhunter_auto_apply.cli upload \
  --job-id <job_id> \
  --selector 'input[type=file]' \
  --file data/output/<job_id>/resume.pdf \
  --approved
```

Submit only after explicit approval:

```bash
python -m jobhunter_auto_apply.cli submit \
  --job-id <job_id> \
  --selector 'button[type=submit]' \
  --approved
```

## Repository layout

| Path | Purpose |
|---|---|
| `scraper.py` | Job collection, scoring, DB utilities, Telegram helpers |
| `callback_handler.py` | Telegram button handler |
| `render_pdf.py` | Resume and cover-letter PDF renderer |
| `jobhunter_auto_apply/` | Approval-gated browser apply helpers |
| `jobhunter_integrations/google_tracker.py` | Open-source-safe Google Sheets/Drive tracker sync CLI |
| `jobhunter_integrations/gmail_watcher.py` | Open-source-safe Gmail watcher CLI for recruiter/ATS replies |
| `job-hunter.skill.md` | Hermes skill/runbook for this project |
| `setup.md` | Full setup guide |
| `data/master-profile.example.json` | Safe candidate-profile schema example |
| `tests/` | Test suite |

## Application tracker

JobHunter can maintain a Google Sheet tracker that mirrors local application state from SQLite. The Sheet is intended for humans; the database remains the automation source of truth.

Typical tracker fields include application date, status, company/title, platform, job/application URLs, linked resume, linked cover letter, linked evidence screenshot, notes, and next action.

Recommended permissions:

1. Main/personal Google account creates or owns the Sheet.
2. Dedicated jobs Gmail is granted **Editor** on the Sheet.
3. JobHunter OAuth for the jobs Gmail includes Gmail scopes plus `spreadsheets` and `drive.file`.
4. Files uploaded by the jobs Gmail, such as resumes, cover letters, and screenshots, must also be shared with the main/personal Google account so the owner can open links from the Sheet.

Recommended formatting includes wrapped text, taller rows, frozen headers, `dd/mm/yyyy hh:mm`-style dates, and status colors such as green for `submitted`, red for blockers/failures, blue for package/draft states, purple for `interested`, and grey for `skipped`.

Run a tracker sync:

```bash
python -m jobhunter_integrations.google_tracker \
  --spreadsheet-id "$JOBHUNTER_TRACKER_SPREADSHEET_ID" \
  --google-token "$GOOGLE_TOKEN_PATH"
```

Run the Gmail watcher once:

```bash
python -m jobhunter_integrations.gmail_watcher \
  --google-token "$GOOGLE_TOKEN_PATH"
```

Both commands are safe to schedule from cron/Hermes cron. They read secrets only from local ignored files/env variables.

## Safety model

JobHunter should never:

- fabricate resume facts;
- guess legal, visa, salary, or eligibility answers;
- bypass CAPTCHA or anti-bot checks;
- print or commit cookies/tokens/passwords;
- submit applications without explicit approval unless a narrow user-defined allowlist exists.

JobHunter should always:

- keep profile data, databases, generated documents, browser profiles, and credentials local;
- record application state in SQLite;
- save evidence screenshots for blockers/draft-ready states when useful;
- ask the user with clear CTA options at approval gates.

## Git hygiene

The repo ignores local runtime/private data, including:

```text
.env
.venv/
browser-profiles/
data/*
!data/master-profile.example.json
*.log
*.backup*
tmp_cdp_*.py
```

Before contributing, run:

```bash
python -m pytest -q tests
git status --short
```
