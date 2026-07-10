# JobHunter setup

JobHunter is an open-source, approval-gated job-search and application assistant. It can scrape/review jobs, send Telegram CTA cards, generate tailored application packages, and prepare browser-based applications while stopping at privacy/legal/final-submit gates.

> Security note: never commit OAuth tokens, client secrets, browser profiles, cookies, generated passwords, resumes, screenshots, databases, or personal profile data. The repo is configured to ignore local runtime data under `data/`, browser profiles, `.env`, logs, and temporary CDP helpers.

## 1. Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Run tests:

```bash
python -m pytest -q tests
```

## 2. Hermes Agent plug-and-play setup

After cloning, start Hermes from the repo root:

```bash
cd JobHunter
hermes
```

Hermes automatically reads `AGENTS.md` as project instructions. A good first prompt is:

```text
Set up JobHunter for me using setup.md. Keep credentials and personal data local, run the tests, and tell me what is missing.
```

The repo also includes `job-hunter.skill.md`. If you want it installed as a reusable Hermes skill outside this repo, copy or install it into your Hermes skills directory according to your Hermes setup.

## 3. Environment variables

Copy `.env.example` to `.env` and fill local secrets:

```bash
cp .env.example .env
```

Required for Telegram notifications/buttons:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Do not commit `.env`.

## 4. Local personal profile data

The open-source repo does not include a real candidate profile. Copy the example schema and fill it locally:

```bash
cp data/master-profile.example.json data/master-profile.json
```

`data/master-profile.json` is ignored by git. It should contain only truthful resume/profile data. The tailoring flow may reorder or emphasize existing facts, but should not invent companies, dates, degrees, skills, or eligibility answers.

## 5. Dedicated application mailbox

Use a dedicated mailbox for ATS registration, verification links, recruiter replies, and approved outbound emails.

Avoid disposable email providers because ATS systems and recruiters may distrust them.

### Gmail options

For consumer Gmail accounts:

1. Prefer Gmail App Password + IMAP/SMTP if App Passwords are available.
2. If App Passwords are unavailable, use OAuth 2.0 Desktop Client.

A Google service account JSON is not enough for a normal `@gmail.com` mailbox. Service accounts can access Gmail user data only when a Google Workspace administrator configures domain-wide delegation.

### Gmail OAuth Desktop Client flow

Google Cloud setup:

1. Create/select a Google Cloud project.
2. Enable Gmail API.
3. Configure OAuth consent screen / Google Auth Platform branding.
4. If the app is in Testing, add the dedicated Gmail account as a test user.
5. Create OAuth client ID with application type **Desktop app**.
6. Download the OAuth client JSON.

Store OAuth files outside the repo, for example:

```text
~/.jobhunter/google_client_secret.json
~/.jobhunter/google_token.json
```

Recommended minimal scopes:

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.modify
```

These allow JobHunter to read verification/reply emails, send approved emails, and mark/label processed messages.

## 6. LinkedIn browser profile

Use a dedicated Chromium profile for LinkedIn automation, not your daily browser profile:

```text
browser-profiles/linkedin
```

Example launch command:

```bash
chromium \
  --user-data-dir="$PWD/browser-profiles/linkedin" \
  --profile-directory=Default \
  --no-first-run \
  --disable-dev-shm-usage \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  https://www.linkedin.com/login
```

The user logs in manually and handles any 2FA/CAPTCHA. Automation later reuses the saved session through Chrome DevTools Protocol (CDP). Never print cookie values, tokens, or localStorage.

## 7. Job recommendation flow

Typical flow:

1. `scraper.py --collect-only` collects candidates into local SQLite.
2. Review/ranking logic filters weak or irrelevant jobs.
3. Telegram sends at most the best 5 CTA job cards per day.
4. User taps **Interested**.
5. `callback_handler.py` marks the job as `interested` and asks the agent to generate a package and prepare the apply draft.

Useful commands:

```bash
python scraper.py --collect-only
python scraper.py --get-job <job_id>
python scraper.py --mark-interested <job_id>
```

## 8. Auto-apply engine

The repo includes a safe, approval-gated engine under:

```text
jobhunter_auto_apply/
```

Main pieces:

| File | Purpose |
|---|---|
| `jobhunter_auto_apply/cdp.py` | Minimal standard-library CDP client for Chromium |
| `jobhunter_auto_apply/engine.py` | Page inspection, approval gates, upload/submit wrappers, DB state recording |
| `jobhunter_auto_apply/vault.py` | Local encrypted ATS credential vault |
| `jobhunter_auto_apply/cli.py` | CLI wrapper around inspection/upload/submit actions |

### Inspect current browser page

Start Chromium with remote debugging, open a LinkedIn/ATS application page, then run:

```bash
python -m jobhunter_auto_apply.cli inspect --job-id <job_id>
```

This records application state and prints a compact page review. It detects common blockers such as CAPTCHA, phone verification, privacy/T&C text, salary, visa/work-authorization, and final-certification language.

### Upload only with approval

```bash
python -m jobhunter_auto_apply.cli upload \
  --job-id <job_id> \
  --selector 'input[type=file]' \
  --file data/output/<job_id>/resume.pdf \
  --approved
```

Without `--approved`, the engine blocks and records `blocked_resume_upload_approval`.

### Submit only with approval

```bash
python -m jobhunter_auto_apply.cli submit \
  --job-id <job_id> \
  --selector 'button[type=submit]' \
  --approved
```

Without `--approved`, the engine blocks and records `blocked_submit_approval`.

## 9. Privacy Notice / Terms & Conditions gates

When an ATS asks for a Privacy Notice, Terms & Conditions, certification, or similar legal acknowledgement:

1. Read the linked notice when accessible.
2. Summarize only critical concerns:
   - unusual data sharing
   - long retention
   - background/security checks
   - international data transfers
   - marketing consent
   - automated decision-making
   - broad or unclear consent
3. Ask the user with clear CTA options, for example:
   - **Accept Privacy Notice and continue**
   - **Decline / stop this application**
4. Save the decision in application state before continuing.

## 10. ATS account credentials

Use the encrypted local vault for generated ATS passwords:

```python
from jobhunter_auto_apply.vault import CredentialVault

vault = CredentialVault()
password = vault.put_generated_ats_password("ats/example-company", username="candidate@example.com")
```

Default local paths:

```text
~/.jobhunter/secrets/vault.key
~/.jobhunter/secrets/ats_credentials.json.enc
```

The key and encrypted vault are outside the repo. Do not store generated passwords in plaintext SQLite or Markdown.

## 11. Application states

Recommended non-secret states in SQLite:

```text
interested
package_generated
draft_inspected
draft_ready
blocked_login_required
blocked_profile_share_prompt
blocked_resume_upload_approval
blocked_unknown_questions
blocked_site_challenge
blocked_submit_approval
approved
submitted
failed
```

Stop and ask the user on:

- CAPTCHA/security checks
- phone verification
- unknown legal/visa/work-authorization questions
- salary questions without confirmed defaults
- privacy/T&C/certification gates
- final submit, unless explicitly approved for that exact application

## 12. Git hygiene for open source

Keep these out of git:

```text
.env
.venv/
browser-profiles/
data/
*.log
*.backup
*.backup-*
tmp_cdp_*.py
open-linkedin-profile.sh
```

Before pushing, run:

```bash
git status --short
python -m pytest -q tests
```

Optionally scan tracked files for real secrets or personal data before release.
