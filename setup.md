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

Optional Interested-stage salary target, used in brief research cards and compensation ask guidance:

```text
JOBHUNTER_TARGET_SALARY_AED_MONTHLY=30000
JOBHUNTER_INTERESTED_WEB_RESEARCH=true
JOBHUNTER_WEB_RESEARCH_TIMEOUT=8
```

`JOBHUNTER_INTERESTED_WEB_RESEARCH` does best-effort company/recruiter/salary web lookup after **Interested**. It is warning-only and falls back to stored metadata if search fails.

Do not commit `.env`.

## 4. Local personal profile data

The open-source repo does not include a real candidate profile. Copy the example schema and keep the working profile local:

```bash
cp data/master-profile.example.json data/master-profile.json
```

`data/master-profile.json` is ignored by git. It should contain only truthful resume/profile data. The tailoring flow may reorder or emphasize existing facts, but should not invent companies, dates, degrees, skills, or eligibility answers.

### Resume Refiner onboarding

Place an existing resume in an ignored path under `data/`, then ask Hermes to run **Resume Refiner** before enabling application-package generation. Do not place personal documents in a tracked directory.

Resume Refiner uses the uploaded resume as a baseline and then interviews the user one experience at a time. It covers:

- role progression, dates, responsibilities, and current support status;
- product purpose, users, scale, and the user's actual contribution;
- languages, frameworks, architecture, data stores, protocols, and integrations;
- migrations, major deliveries, technical decisions, and the reasons behind them;
- production incidents, constraints, diagnosis, solution, and verified result;
- testing levels, CI/CD, deployment ownership, monitoring, and operations;
- collaboration, coordination, mentoring, and architecture responsibility;
- metrics, certifications, side projects, and relevant skills missing from the source resume;
- confidentiality limits and claims the user does not want made.

Hermes must ask one focused question at a time, follow useful threads, and distinguish the user's answer from proposed resume wording. It may store a statement in the usable evidence bank only after the user explicitly confirms that statement. It must not infer missing metrics, dates, technologies, ownership, or impact.

Draft answers and progress belong in an ignored local refiner-session file. Accepted updates must be applied with `atomic_update_profile(..., candidate_confirmed=True)` only after the user confirms the exact facts and wording. The helper preserves existing profile data, makes a timestamped backup, and atomically replaces the profile. The user may pause and resume the interview.

Validate the result before tailoring:

```bash
python resume_refiner.py validate data/master-profile.json
```

Only evidence marked `candidate-confirmed`, `public`, and visible to `resume` or `cover-letter` is eligible for generated documents. Private, draft, rejected, or interview-only notes are excluded from the public application package.

## 5. Dedicated application mailbox and dual-account model

Use a dedicated jobs/agent mailbox for ATS registration, verification links, recruiter replies, and approved outbound emails.

Avoid disposable email providers because ATS systems and recruiters may distrust them.

JobHunter works best with **two Google accounts**:

| Account | Purpose |
|---|---|
| Main/personal Google account | Human-owned account. Owns/views the tracker sheet and Drive evidence folder. |
| Dedicated jobs/agent Gmail account | Automation account used by JobHunter for Gmail, Sheets, and Drive API calls. |

Recommended pattern:

1. Create the tracker spreadsheet from the main/personal account.
2. Share that spreadsheet with the dedicated jobs Gmail as **Editor**.
3. Authorize JobHunter OAuth using the dedicated jobs Gmail account, not the personal account.
4. When JobHunter creates/uploads files into a Drive evidence folder through the jobs Gmail, grant the main/personal account access to that folder/files. Otherwise the tracker may contain Drive links that the human owner cannot open.

### Gmail options

For consumer Gmail accounts:

1. Prefer Gmail App Password + IMAP/SMTP if App Passwords are available.
2. If App Passwords are unavailable, use OAuth 2.0 Desktop Client.

A Google service account JSON is not enough for a normal `@gmail.com` mailbox. Service accounts can access Gmail user data only when a Google Workspace administrator configures domain-wide delegation.

### Gmail OAuth Desktop Client flow

Google Cloud setup:

1. Create/select a Google Cloud project.
2. Enable the required APIs:
   - Gmail API
   - Google Sheets API, if using the application tracker
   - Google Drive API, if uploading/linking resumes, cover letters, or screenshots
3. Configure OAuth consent screen / Google Auth Platform branding.
4. If the app is in Testing, add the dedicated Gmail account as a test user.
5. Create OAuth client ID with application type **Desktop app**.
6. Download the OAuth client JSON.

Store OAuth files outside the repo, for example:

```text
~/.jobhunter/google_client_secret.json
~/.jobhunter/google_token.json
```

Recommended Gmail-only scopes:

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.modify
```

These allow JobHunter to read verification/reply emails, send approved emails, and mark/label processed messages.

If using the shared Google Sheet application tracker, add:

```text
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/drive.file
```

`spreadsheets` allows JobHunter to update tracker rows. `drive.file` allows JobHunter to create/upload the specific Drive files it manages, such as uploaded evidence screenshots, sent resumes, and sent cover letters. After the jobs Gmail creates a Drive evidence folder, make sure the main/personal Google account has access to that folder/files so the human owner can open the tracker links.

### Gmail watcher

After OAuth is configured, run the repo-provided watcher module:

```bash
python -m jobhunter_integrations.gmail_watcher \
  --google-token "$GOOGLE_TOKEN_PATH"
```

It prints nothing when there is nothing new to report, so it is safe for script-only cron jobs. It marks every inspected message as read to keep the jobs mailbox clean. Schedule it against the dedicated jobs Gmail account, for example at 10:00 and 15:00.

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
2. Interested/Skip feedback is summarized from the `job_feedback` table.
3. Review/ranking logic uses that feedback to demote repeatedly declined patterns and boost similar interested matches.
4. Telegram sends at most the best 5 CTA job cards per day.
5. User taps **Interested** or **Skip**.
6. **Interested** records feedback/application state and sends a concise research brief: company context, recruiter/poster if known, warning-only legitimacy notes, and salary guidance vs `JOBHUNTER_TARGET_SALARY_AED_MONTHLY`.
7. The research brief offers **Apply**, **Ignore**, and **Details** CTAs.
8. **Apply** generates a truthful resume + cover-letter package from `data/master-profile.json`, records `package_generated`, and sends a final **Proceed to apply** / **Pause** CTA.
9. **Proceed to apply** starts application preparation only. Final submit, CAPTCHA, legal/visa/salary questions, and sensitive confirmations remain approval-gated.

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

### Human CAPTCHA handoff from a phone

JobHunter must not bypass CAPTCHA, phone verification, identity verification, or other anti-bot checks. When an ATS blocks on one of these checks, pause automation and let the human complete it on the live browser session.

If JobHunter runs on a Raspberry Pi or another always-on desktop machine, the user can connect from a phone with VNC:

1. Enable VNC/remote desktop on the machine that owns the Chromium profile. On Raspberry Pi OS Bookworm this is commonly `wayvnc`; older images may use RealVNC or `x11vnc`.
2. Find the machine address:

   ```bash
   hostname -I
   ```

3. From the phone, install a VNC client such as **RealVNC Viewer**, **Screens**, or **bVNC**.
4. Connect to `<machine-ip>:5900` on the same LAN. If away from home, connect over a private VPN such as Tailscale and use the machine's VPN IP instead of exposing VNC to the public internet.
5. Complete the CAPTCHA/verification manually in the visible Chromium window.
6. Tell the agent the human check is complete so it can inspect the result, continue if approved, and record the application state/evidence.

Security notes:

- Use a VNC password or OS login; never expose VNC directly to the public internet.
- Prefer LAN or VPN-only access.
- Do not share browser profiles, cookies, or remote-debugging ports outside the machine.
- The agent may continue after the user finishes the CAPTCHA, but it should never solve the CAPTCHA itself.

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

## 12. Shared application tracker

JobHunter can sync application state to a shared Google Sheet for easy human access.

Recommended tracker columns:

```text
Applied At
Last Updated
Status
Job Title
Company
Platform
Job URL
Application URL
Resume Sent
Cover Letter Sent
Package Folder
Evidence Screenshot
Notes
Next Action
```

Recommended setup:

1. Create a Google Sheet from the main/personal Google account.
2. Share it with the dedicated jobs Gmail account as **Editor**.
3. Store the spreadsheet ID in local config outside the repo, for example under `~/.hermes/state/` or `~/.jobhunter/`.
4. Run the repo-provided sync module using the jobs Gmail OAuth token:

   ```bash
   python -m jobhunter_integrations.google_tracker \
     --spreadsheet-id "$JOBHUNTER_TRACKER_SPREADSHEET_ID" \
     --google-token "$GOOGLE_TOKEN_PATH"
   ```

5. To keep the tracker live while applying, enable best-effort auto-sync in local `.env` or deployment env:

   ```env
   JOBHUNTER_AUTO_SYNC_TRACKER=true
   JOBHUNTER_TRACKER_SYNC_COMMAND=/absolute/path/to/jobhunter_sync_application_tracker.sh
   JOBHUNTER_TRACKER_SYNC_TIMEOUT=120
   ```

6. Upload evidence screenshots, sent resumes, and sent cover letters to a Drive folder created/managed by the jobs Gmail.
7. Grant the main/personal Google account access to that Drive folder/files. This is required so the human owner can click `Open resume`, `Open cover letter`, and `Open screenshot` links from the Sheet.

Useful formatting for the tracker:

- date strings like `18/07/2026 15:47`;
- wrapped text;
- taller rows;
- auto-sized columns;
- frozen header row;
- status colors, for example:
  - `submitted` → green
  - `blocked_*`, `failed`, `unavailable` → soft red
  - `package_generated`, `package_prepared`, `draft_ready`, `approved` → blue
  - `interested` → purple
  - `skipped` → grey

Keep the Sheet as a human-friendly mirror. SQLite remains the source of truth for automation.

## 13. Git hygiene for open source

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
