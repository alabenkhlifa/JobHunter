# JobHunter setup notes

This document captures the safe setup flow used to connect JobHunter to a dedicated Gmail mailbox and a logged-in LinkedIn browser profile for approval-gated job applications.

> Security note: do **not** commit OAuth tokens, client secrets, service-account keys, browser profiles, cookies, generated passwords, or application evidence screenshots. Keep those in local ignored paths or a secret manager.

## 1. Dedicated Gmail mailbox

Create a dedicated mailbox for JobHunter, for example:

```text
jobs.example@gmail.com
```

This mailbox is used for:

- ATS account creation and verification links
- recruiter/application replies
- rejection/interview notifications
- sending application emails when explicitly approved

Avoid temporary/disposable email providers for job applications because ATS systems and recruiters may distrust or block them.

## 2. Why Gmail service accounts are not enough

A Google service account JSON is **not** the right credential for a normal consumer Gmail account.

Service accounts can access Gmail user data only when a Google Workspace administrator configures **domain-wide delegation**. A normal `@gmail.com` account has no Workspace admin who can grant that delegation.

For consumer Gmail accounts, use one of these instead:

1. Gmail App Password via IMAP/SMTP, if App Passwords are available.
2. OAuth 2.0 Desktop Client, if App Passwords are unavailable or API access is preferred.

## 3. Gmail OAuth setup used here

App Passwords were unavailable for the dedicated Gmail account, so we used a Google OAuth Desktop Client.

### Google Cloud setup

1. Open Google Cloud Console and create/select a project.
2. Enable the Gmail API:

   ```text
   https://console.cloud.google.com/apis/library/gmail.googleapis.com
   ```

3. Configure the OAuth consent screen / Google Auth Platform branding:
   - App name: `JobHunter`
   - User support email: the dedicated Gmail account
   - Developer contact email: the dedicated Gmail account or maintainer email
   - Audience: External
   - Publishing status can be Testing or Published

4. If the app is in Testing, add the dedicated Gmail account as a test user.
5. Create an OAuth client:
   - Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - Name: for example `JobHunter Hermes Pi`
6. Download the OAuth client JSON.

Do **not** use a service-account key for a consumer Gmail mailbox.

### Local OAuth files

Store the downloaded OAuth client JSON outside git, for example:

```text
~/.hermes/google_client_secret.json
```

The resulting OAuth refresh token is also local-only, for example:

```text
~/.hermes/google_token.json
```

Both files must remain secret and must not be committed.

### Requested Gmail scopes

Use the smallest useful Gmail scope set:

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.modify
```

These allow JobHunter to:

- search/read incoming ATS and recruiter messages
- send emails when approved
- mark/label messages as processed

Do not request Calendar, Drive, Docs, or Sheets scopes unless the product actually needs them.

### OAuth authorization flow

Generate an authorization URL with:

- `response_type=code`
- OAuth desktop client ID
- redirect URI matching the client JSON, e.g. `http://localhost`
- Gmail scopes listed above
- `access_type=offline`
- `prompt=consent`
- PKCE `code_challenge` / `code_verifier`

Open the URL while logged into the dedicated Gmail account. After approval, Google redirects to a localhost URL like:

```text
http://localhost/?state=...&code=...&scope=...
```

The page may fail to load because no local web server is listening. That is expected. Copy the full redirected URL and exchange its `code` for tokens using the saved PKCE verifier.

After exchange, verify the live Gmail profile:

```python
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

creds = Credentials.from_authorized_user_file("~/.hermes/google_token.json")
service = build("gmail", "v1", credentials=creds)
profile = service.users().getProfile(userId="me").execute()
print(profile["emailAddress"])
```

Expected result: the dedicated JobHunter Gmail address.

## 4. LinkedIn persistent browser profile

Use a dedicated Chromium profile for LinkedIn automation, not the user's normal browser profile:

```text
/home/<user>/JobHunter/browser-profiles/linkedin
```

Launch Chromium with that profile:

```bash
chromium \
  --user-data-dir=/home/<user>/JobHunter/browser-profiles/linkedin \
  --profile-directory=Default \
  --no-first-run \
  --disable-dev-shm-usage \
  https://www.linkedin.com/login
```

The user logs in manually through VNC/desktop access and handles any 2FA/CAPTCHA. The automation later reuses the saved browser session.

Verification should check only login state, not print cookies or tokens. For example, verify that the profile contains LinkedIn auth cookie names and that an authenticated LinkedIn page loads. Never log cookie values.

## 5. Safe LinkedIn application flow

When a user marks a job as interested:

1. Create/update an `applications` row with stage `interested`.
2. Generate tailored resume and cover letter.
3. Open the LinkedIn job using the persistent browser profile.
4. Detect apply type:
   - Easy Apply
   - external/company apply
   - blocked/login/CAPTCHA/unknown question
5. Stop before sensitive actions unless the user has approved them.

For external apply jobs, LinkedIn may show a prompt like:

```text
Share your profile?
```

Treat this as a user-approval gate. Only click Continue after explicit approval, because it shares the user's LinkedIn profile with the job poster.

## 6. External ATS account handling

Some external ATS pages require registration before applying. The intended design is:

- create ATS accounts only when needed
- use the dedicated JobHunter Gmail mailbox
- generate a strong unique password per ATS/company
- store credentials in an encrypted local vault, not in plaintext SQLite
- use the JobHunter database only for non-secret references and workflow state

Recommended non-secret application states include:

```text
interested
package_generated
draft_ready
blocked_login_required
blocked_profile_share_prompt
blocked_resume_upload_approval
blocked_unknown_question
blocked_captcha
approved
submitted
failed
```

Stop and ask the user on:

- CAPTCHA/security checks
- phone verification
- unknown legal/visa/work-authorization questions
- salary questions without confirmed defaults
- final submit, unless the platform/company is explicitly allowlisted

## 7. Local files that should be ignored

Add/keep these out of git:

```text
browser-profiles/
*.sqlite-wal
*.sqlite-shm
/home/*/.hermes/google_client_secret.json
/home/*/.hermes/google_token.json
/home/*/.hermes/google_oauth_pending.json
/home/*/.hermes/google-workspace-venv/
data/output/
data/*.db
data/*.log
```

For an open-source release, document environment variables and setup steps, but never include real credentials, tokens, cookies, generated resumes, screenshots, or personal profile data.
