# Agent instructions for JobHunter

This repository is intended to be operated by Hermes Agent or another coding agent. Follow these rules when setting up, modifying, or running it.

## Safety first

- Never commit or print real credentials, cookies, OAuth codes, refresh tokens, generated ATS passwords, browser profiles, resumes, screenshots, or SQLite databases.
- Keep candidate data local in `data/master-profile.json`; use `data/master-profile.example.json` for documentation/tests/examples.
- For Google integrations, use `jobhunter_integrations.google_tracker` and `jobhunter_integrations.gmail_watcher` with local ignored OAuth token/config paths; never hardcode real spreadsheet IDs, Gmail addresses, or machine paths in tracked code.
- Do not fabricate resume facts, employment dates, degrees, skills, certifications, salary, work authorization, visa status, or legal declarations.
- Do not bypass CAPTCHA, anti-bot checks, phone verification, identity verification, or suspicious sites.
- Stop before final submit unless the user explicitly approves that exact application.
- For Privacy Notices / Terms & Conditions, read the notice when accessible, summarize only critical concerns, then ask with CTA options: accept or decline.

## Expected setup flow

1. Read `setup.md` and this file.
2. Create a virtual environment and install `requirements.txt`.
3. Copy `.env.example` to `.env` and ask the user to provide Telegram values if needed.
4. Copy `data/master-profile.example.json` to `data/master-profile.json` and ask the user to fill truthful profile details.
5. Run `python -m pytest -q tests`.
6. If using browser apply, launch Chromium with CDP on `127.0.0.1:9222` and a dedicated profile under `browser-profiles/`.

## Auto-apply workflow

Use `jobhunter_auto_apply` conservatively:

- `python -m jobhunter_auto_apply.cli inspect --job-id <job_id>` to inspect current ATS/browser page.
- `upload ... --approved` only after explicit approval to upload a document.
- `submit ... --approved` only after explicit approval to submit.
- Record states through the existing application tracking functions in `scraper.py`.

When missing fields appear, inspect local profile data and cached confirmed answers first. Ask only for unconfirmed or sensitive fields, with clear CTA-style options.

## Development rules

- Prefer small, testable changes.
- Add/update tests for new behavior.
- Run `python -m pytest -q tests` before claiming success.
- Keep Git clean; only commit open-source-safe files.
- If you change setup behavior, update `setup.md` and `README.md` together.
