# JobHunter

JobHunter is an approval-gated job-search and application assistant designed to work well with [Hermes Agent](https://hermes-agent.nousresearch.com/).

It can:

- collect job opportunities from supported sources;
- store and deduplicate jobs in local SQLite;
- review candidates across markets and send Telegram digests within the configured delivery cap;
- learn from Interested/Skip feedback to demote repeatedly declined patterns and boost similar strong matches;
- run a brief Interested-stage company/recruiter/salary research check before generating an application package;
- run a resumable Resume Refiner interview that turns an uploaded resume and confirmed follow-up answers into a detailed evidence bank;
- generate truthful tailored resume / cover-letter packages from a local candidate profile;
- inspect LinkedIn / external ATS application pages through Chromium CDP;
- upload or submit only after explicit user approval;
- monitor a dedicated jobs Gmail mailbox and classify rejection, interview, assessment, action, progression, and offer replies;
- sync an application tracker to a shared Google Sheet, including status, dates, resume/cover-letter links, and evidence screenshots;
- store generated ATS credentials in a local encrypted vault.

JobHunter is intentionally **not** a blind mass-apply bot. It stops on privacy notices, T&C, salary, visa/work authorization, CAPTCHA/security checks, and final submit unless the user explicitly approves.

![Sanitized Telegram job recommendation card example](assets/telegram-job-card-example.svg)

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

Before enabling application-package generation, place your existing resume in an ignored path under `data/` and ask Hermes to run **Resume Refiner**. Hermes reviews the imported facts with you, interviews you experience by experience, and stores only facts and resume wording you explicitly confirm. The interview can be paused and resumed.

Resume Refiner can also store candidate-confirmed role-family variants, such as JVM-backend or software-architect versions. A variant is a complete public resume snapshot containing the exact approved headline, summary, skills, certifications, experience selection, education, ordering, section omissions, matching terms, optional title-scoped `role_terms`, and optional page limit. JobHunter combines it only with the current identity and contact fields; unspecified master-profile sections are not inherited. Draft or unconfirmed variants are ignored. Most jobs use evidence ranking when no variant matches, but architecture-titled jobs pause until a matching role-scoped variant is confirmed. Package generation also pauses for obvious inconsistent role chronology rather than guessing a date.

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

For a new profile, use:

```text
Run Resume Refiner on the resume I placed under data/. Ask me one focused question at a time, keep every claim truthful, and update the local master profile only after I confirm each proposed fact.
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

The Hermes daily collector balances up to 40 review candidates across available markets after applying feedback. Unreviewed candidates precede repeated holds. Delivery combines the new approvals with still-eligible approvals from earlier batches; the default is three places per market with unused places shared, up to 12 jobs. Freshness, score and hard filters still apply, and a held job needs a new approval before it can enter a digest.

Before delivery, JobHunter checks the source listing for the same role and current application availability. Confirmed closure marks an unsent job `unavailable`; timeouts, blocked pages and ambiguous results remain pending for retry. Checks also run for individual cards, delivery retries and the owner's “more” list:

```bash
python scraper.py --list-queued --limit 10
```

This list contains eligible, unsent listings confirmed open during the check; entries may still need a fit review. An empty list does not prove there are no jobs in that market.

Inspect a job:

```bash
python scraper.py --get-job <job_id>
```

Mark a job as interested:

```bash
python scraper.py --mark-interested <job_id>
```

Telegram CTA flow:

```text
Interested → brief research card → Apply / Ignore / Details
Apply      → validate chronology and role-variant gate → select confirmed variant or legacy tailoring → generate package + manifest → Proceed to apply / Pause
Blocked    → generate nothing and record no package stage → Refine resume / Pause
Proceed   → begin application prep only; final submit remains explicitly approval-gated
```

The research card is intentionally brief and warning-only. It does a best-effort web lookup for company/recruiter/salary context, then falls back to stored metadata if search fails. It summarizes company context, recruiter/poster info when stored, obvious legitimacy concerns, and a salary note compared with the configurable monthly AED target:

```env
JOBHUNTER_TARGET_SALARY_AED_MONTHLY=30000
JOBHUNTER_INTERESTED_WEB_RESEARCH=true
JOBHUNTER_WEB_RESEARCH_TIMEOUT=8
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
| `resume_refiner.py` | Refined-profile validation, confirmed-evidence handling, and safe atomic profile updates |
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

![Sanitized application tracker sheet example](assets/application-tracker-example.svg)

The tracker can also link generated artifacts and evidence files so the main account can open application packages without accessing local runtime data directly.

Set `JOBHUNTER_TRACKER_SHARE_WITH` to the personal Google account that opens these links (comma-separated for multiple named readers). Sync grants read access to the managed evidence folder without notification emails; current and future files inherit that access. The folder stays restricted to its owner and named users. The setting belongs in the private `.env` and encrypted recovery.

![Sanitized tracker attachment links and screenshot preview](assets/application-tracker-attachments-example.svg)

Recommended permissions:

1. Main/personal Google account creates or owns the Sheet.
2. Dedicated jobs Gmail is granted **Editor** on the Sheet.
3. Authorize a separate tracker token with `spreadsheets` and `drive.file`; keep the mailbox token read-only.
4. Files uploaded by the jobs Gmail, such as resumes, cover letters, and screenshots, must also be shared with the main/personal Google account so the owner can open links from the Sheet.

Recommended formatting includes wrapped text, taller rows, frozen headers, `dd/mm/yyyy hh:mm`-style dates, and a non-white color for every application row: green for `submitted`/`offer_received`, red for `rejected`/failures, orange for blockers, blue for application progression/interviews/package states, amber for assessments/actions, purple for `interested`, grey for closed/skipped states, and light blue-grey for new or unknown statuses.

The tracker preserves its 14 columns in the order shown above. The reference palette is header `#E2ECFD`, submitted `#D7EED3`, interested `#E6DBF7`, preparation/progression `#D5E4FC`, blocked `#F7E1C3`, and rejected `#F3D4CD`. Status colors cover the full row from A through N.

Authorize the tracker using the existing Desktop client and configured jobs account:

```bash
python -m jobhunter_integrations.google_tracker_auth authorize
python -m jobhunter_integrations.google_tracker_auth check --refresh \
  --spreadsheet-id "$JOBHUNTER_TRACKER_SPREADSHEET_ID" \
  --sheet-id "$JOBHUNTER_TRACKER_SHEET_ID"
```

The tracker token defaults to `~/.jobhunter/google_tracker_token.json`. The command verifies the Drive account before saving credentials and refuses to overwrite the mailbox token. Enable the Sheets and Drive APIs in the same Google Cloud project. The check reads the existing tab's title and headers without editing cells; `--sheet-id` is the `gid` from its URL. It confirms read access, not edit permission. Keep auto-sync disabled until the existing tracker data and target tab have been reviewed. Back up this separate token with the existing encrypted credential recovery.

Run a tracker sync after that review:

```bash
python -m jobhunter_integrations.google_tracker \
  --spreadsheet-id "$JOBHUNTER_TRACKER_SPREADSHEET_ID" \
  --google-token "$JOBHUNTER_TRACKER_GOOGLE_TOKEN_PATH"
```

Add `--dry-run` to preview row counts without updating Sheets or uploading files. Sync merges into the existing tab, keeps unmatched history and newer spreadsheet statuses, preserves existing document links and manual notes, and orders whole rows by **Applied At, newest first**. Ambiguous matches stop the sync. Only changed cells are written, with formatting and row moves in one Sheets batch; the tab is never cleared. `Last Updated` reflects the database's application update time, not each sync's run time. Private state includes the uploaded-file cache, last sync receipt and the sheet snapshot from before the last write.

Authorize the dedicated Gmail account and test refresh first:

```bash
python -m jobhunter_integrations.gmail_auth authorize
python -m jobhunter_integrations.gmail_auth check --refresh
```

Use your existing Google **Desktop app** client JSON and set `JOBHUNTER_GMAIL_ACCOUNT` in `.env`. Setup requests Gmail read permission only, uses a loopback callback with PKCE, and refuses a mismatched mailbox. See [setup.md](setup.md#gmail-oauth-desktop-client-flow) for headless SSH consent and recovery. Use a Production consent audience for ongoing use; Gmail grants from External apps in Testing expire after seven days. OAuth credentials and the processed-mail ledger belong in an encrypted backup, never plaintext Git. Restore them and check refresh before resuming monitoring. The Pi config repo manages them through its secret manifest and `secrets.age`.

Run the Gmail watcher once:

```bash
python -m jobhunter_integrations.gmail_watcher \
  --google-token "$GOOGLE_TOKEN_PATH"
```

These integrations read credentials from ignored files/env variables. The Gmail watcher tracks processed IDs and leaves mail read flags unchanged. When recognized rejection, interview, assessment, action-required, progression, or offer language matches exactly one active application, it updates its status and prints an alert. Basic acknowledgements remain `submitted`; ambiguous matches do not change application state. Recognized verification messages are excluded from periodic alerts. It never replies, follows links, completes assessments or accepts offers. Schedule only after the connection check passes, avoid overlapping runs, and arrange delivery of nonempty output and failures. Sheets/Drive and sending access remain separate optional authorizations.

For a code requested during an approved ATS interaction, use `python -m jobhunter_integrations.gmail_verification --sender-domain <expected-domain> --after <request-timestamp>`. It verifies recipient, exact sender domain and a window of at most 15 minutes, then saves the newest matching email to a private file without printing its contents. Read it only for that interaction and delete it afterwards; never forward codes to Telegram. See [setup.md](setup.md#ats-verification-during-an-approved-application).

For scheduled Telegram alerts, use `python -m jobhunter_integrations.gmail_monitor`. It keeps a private outbox, retries unconfirmed delivery, commits processed IDs after confirmed delivery, and prevents concurrent monitor runs. Success is silent to avoid a second cron summary. Configure Hermes with a script-only job (`no_agent=true`) at `0 10,15 * * *` in the intended timezone, with Telegram delivery for failures. The Pi config repo tracks the entrypoint and job definition. Registration and verification-code lookup remain on demand within the approved application; the periodic job only handles replies.

Reply alerts show the classified outcome, matched company and role, and whether the database and spreadsheet update succeeded. Receipt acknowledgements never reset an application's status. Unrecognized outcomes are labeled **Review needed**, with an explicit warning that the status was unchanged; classification rules do not cover every employer's wording. Known outcomes omit the email's generic opening snippet.

To keep the Sheet live while applying, enable best-effort auto-sync after every application stage update:

```env
JOBHUNTER_AUTO_SYNC_TRACKER=true
JOBHUNTER_TRACKER_SYNC_COMMAND=/absolute/path/to/jobhunter_sync_application_tracker.sh
JOBHUNTER_TRACKER_SYNC_TIMEOUT=120
```

Auto-sync failures are logged but do not block application progress; SQLite remains the source of truth.

When enabled, the mail monitor retries tracker sync after every scheduled check, including checks with no new replies. An unsuccessful retry is reported through the cron job's failure delivery. Back up the tracker token and `tracker_drive_files.json`, `tracker_sync_state.json`, and `tracker_before_sync.json` from the private state directory in encrypted recovery.

## Safety model

JobHunter should never:

- fabricate resume facts;
- guess legal, visa, salary, or eligibility answers;
- bypass CAPTCHA or anti-bot checks;
- print or commit cookies/tokens/passwords;
- submit applications without explicit approval unless a narrow user-defined allowlist exists.

JobHunter should always:

- keep profile data, databases, generated documents, browser profiles, and credentials local;
- use only candidate-confirmed public evidence from Resume Refiner in generated application documents;
- record application state in SQLite;
- save evidence screenshots for blockers/draft-ready states when useful;
- ask the user with clear CTA options at approval gates.

For CAPTCHA or other human-verification blockers, pause automation and let the user complete the challenge in the live browser. For the legacy owner deployment, a practical remote-handoff pattern is VNC from a phone: enable VNC/remote desktop on the Pi, connect with a mobile VNC app to `<pi-lan-or-vpn-ip>:5900`, complete the CAPTCHA manually, then tell the agent to continue. Use LAN/VPN access only; never expose VNC, cookies, browser profiles, or CDP ports to the public internet.

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

## Invited Telegram profiles

The optional `jobhunter_service` supports invited candidates through the owner's existing Telegram bot. Hermes remains the sole Telegram receiver and forwards candidate updates to a restricted service before its administrative handlers. The owner's normal conversations, CLI, cron jobs and browser keep their existing behavior. The owner registers a **numeric Telegram user ID** through `/jobhunter add <id>` or the Pi's `jobhunter-admin` Hermes skill. A candidate activates the invitation by starting a private conversation with the same bot. Keep the administrative Hermes allowlist restricted to the owner.

Candidates upload a PDF, DOCX or text resume and refine it conversationally with a restricted Hermes planner. Each configuration change receives an exact preview and a confirmation button. The backend binds identity to the Telegram sender; model output cannot register users, execute commands, confirm facts or access another profile. Search settings, credentials, job history, documents, schedules and delivery queues live in private `u<telegram_id>` directories beneath the service data root.

Candidates can set minimum and maximum advertised experience requirements and preferred experience ranges. They can also choose a standard or compact Telegram digest, group jobs by destination, and show advertised salary or the review's match reason. Formatting preferences do not change job eligibility or application approvals.

Each destination declares work authorization (`authorized`, `sponsorship_required`, or `unknown`) and relocation needs. Authorization allows jobs without sponsorship; candidates requiring sponsorship receive jobs whose review confirms an offer of sponsorship. Unknown authorization stays held. Visa-free entry alone does not establish work authorization. Optional salary targets are guidance and do not silently reject jobs. Each candidate chooses an IANA timezone, local time and weekdays. Searches are queued independently and run serially on the Pi; a long collection can delay execution, while other candidates' scheduled slots are retained.

Google integrations are optional and independently authorized. Recommend a dedicated jobs Gmail owning a newly created Google Sheet and document folder, shared as reader with the candidate's personal account. A personal Gmail is also supported; tracker and monitored mailbox can differ. `/connect tracker` creates the tracker after consent. `/connect gmail` enables mailbox access after the candidate has configured monitoring. The Sheet can be viewed in the browser or downloaded from Google Sheets as Excel.

`/jobs` lists collected roles. `/details <id>`, `/interested <id>` and `/apply <id>` show a role, track interest and prepare private application documents. `/connect linkedin` opens a short-lived authenticated web viewer for the candidate's own container browser. Candidates sign in directly on LinkedIn and complete human verification themselves. They do not receive a Pi desktop or VNC account. `/inspect <id>` checks the current application; `/upload <id>` and `/submit <id>` each require a separate confirmation bound to the candidate, job, page, browser and document. A click alone records an attempted submission, and uncertain outcomes prevent automatic retries. `/tracker` retries synchronization; `/logout` closes the viewer while retaining the private browser login profile.

Owner suspension/revocation pauses searches and invalidates access links, pending deliveries and approvals. Candidate data remains available for controlled recovery. Telegram delivery uses a durable outbox and marks jobs notified only after a full destination acknowledges every message part. A lost provider acknowledgement can still cause a duplicate message on retry; the Bot API offers no transactional delivery with the local database.

Each outbox retry checks listing availability again. An inconclusive check keeps the unsent remainder pending. Confirmed closure cancels the remaining copies of that digest; it does not send an outdated listing to another destination.

See [setup.md](setup.md#14-restricted-multi-user-service-on-the-pi) for installation, required operator settings and recovery. Telegram onboarding reuses the owner's bot and model login. Google consent and private browser login additionally require a reachable HTTPS endpoint; Google integrations also need a web OAuth client.

The HTTPS address belongs to the JobHunter service on the Pi. Candidates do not need a personal website or domain.

The Pi deployment supports Tailscale Funnel for this address. A loopback gateway exposes only the private login/consent/viewer routes; administrative APIs remain local. Friends open `/connect linkedin` links from their private Telegram chat in an ordinary browser, without installing Tailscale or receiving access to the owner's desktop. The browser stores each LinkedIn login in that candidate's own profile. HTTPS activation and deployment verification are described in [setup.md](setup.md#14-restricted-multi-user-service-on-the-pi).

An owner-only local credential service resolves the current Hermes model/provider and refreshes the original credential pool. The restricted planner receives only the effective access credential in memory, including support for the owner's `openai-codex` OAuth login. Each request runs in a disposable Hermes home with tools, owner files, memory and history disabled. Candidate updates are acknowledged after durable local storage and retried independently of the owner's Telegram polling offset. A standalone bot and explicit API-key provider remain optional deployment modes.
