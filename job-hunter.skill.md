---
name: job-hunter
description: Automated job search agent for Dubai market.
  Scrapes LinkedIn and Foundit Gulf, scores matches against a
  Software Architect / Tech Lead / Senior Engineer backend profile, and notifies via Telegram.
triggers:
  - job search
  - find jobs
  - job hunt
  - new jobs
  - interested in job
---

# Job Hunter Skill

## Overview
This skill automates job searching for Software Architect / Cloud Architect /
Tech Lead / Senior Engineer backend roles in Dubai. It scrapes LinkedIn (guest API) and
Foundit Gulf (JSON API), stores keyword-qualified candidates, then a Hermes
cron job reviews them with an LLM before suggesting offers.

## Architecture
- **Scraper**: `scraper.py` — scraping + CLI utilities (get-job, send-doc, send-msg, mark-interested)
- **Renderer**: `render_pdf.py` — dumb PDF renderer for resume and cover letter
- **Auto-apply engine**: `jobhunter_auto_apply/` — approval-gated browser/ATS inspection, upload/submit wrappers, and encrypted ATS credential vault
- **Profile**: `data/master-profile.json` — local ignored master resume data (never fabricated); `data/master-profile.example.json` documents the schema
- **Sources**: LinkedIn (guest HTML API), Foundit Gulf (JSON middleware API)
- **Storage**: SQLite for deduplication, job state, application state, and confirmed answer cache
- **Notifications**: Telegram Bot API (HTML parse mode)
- **Designed for**: local/Hermes operation with optional cron and Chromium CDP for browser apply flows

## Scraping Strategy
The scraper uses **breadth-first round-robin** across 2 buckets:
- LinkedIn/Dubai, Foundit/Dubai
- Collects **1 matching job per bucket** (2 total)
- Fetches page 1 of every keyword before going to page 2
- Evaluates jobs after each page fetch to stop early
- Scrapers are generators that yield one page at a time

### Search Keywords
- software architect, cloud architect, tech lead
- lead software engineer, senior software engineer, senior backend engineer
- platform architect, solutions architect

### Regions
- **Dubai only**: searches "Dubai" and filters out returned jobs whose displayed location does not include Dubai

## Scoring System
Jobs are scored by matching keywords in title + company + full description:
- **High (+3)**: architect, aws, azure, spring boot, microservices, tech lead,
  team lead, java, kotlin, backend
- **Medium (+1)**: docker, ci/cd, kubernetes, terraform, cloud, .net,
  typescript, devops, infrastructure
- **Threshold**: score >= 13 to qualify as a match

## Filters (applied before scoring)
1. **Excluded titles**: test engineer, qa, staff software engineer, sdet,
   machine learning, ml engineer, ml architect
2. **Job age**: posted within last 7 days only
3. **Location**: only keeps jobs whose displayed location includes Dubai
4. **Local presence**: skips jobs requiring existing UAE/Saudi residency or
   that won't sponsor visas
5. **Experience**: skips jobs requiring more than 8 years

## Job Enrichment
For each candidate job, the scraper fetches the full description and extracts:
- **Tech stack**: split into required vs nice-to-have (parsed from section headers)
- **Min experience**: regex extraction from description
- **Salary**: regex extraction (AED/USD/SAR amounts)
- **Work model**: remote / hybrid / on-site (signal phrase matching)
- **Score breakdown**: lists each matched term with its weight

## Workflow

### When triggered by Hermes cron (scheduled):
1. Run the collector script: `~/.hermes/scripts/jobhunter_collect_candidates.py`
2. The collector runs: `.venv/bin/python3 scraper.py --collect-only`
2. Scraper iterates Dubai buckets only
3. For each new job passing hard filters and keyword score threshold:
   - Saves to SQLite database
   - Does **not** notify directly
4. Hermes cron reviews unnotified candidates with an LLM against Ala's profile,
   feedback-adjusted score, and `feedback_learning_notes`; it rejects low-seniority/student/intern/junior roles, non-Dubai roles,
   local-only/no-relocation roles, and unrelated frontend/QA/data/ML/DevOps-only roles.
5. Hermes sends at most the best 5 human-approved recommendations back to Telegram and marks
   reviewed candidate IDs as `notified=1` to avoid repeats.

### When user replies "interested" for a job:
Hermes/JobHunter handles the intelligent tailoring and safe apply preparation; scripts handle rendering, state tracking, and browser-page inspection.

1. Get job details:
   ```bash
   python3 scraper.py --get-job <job_id>
   ```
   This prints the full job JSON (title, company, description, tech stacks, etc.)

2. Send progress message:
   ```bash
   python3 scraper.py --send-msg "<b>📝 Generating tailored resume and cover letter for:</b>
   <b><job_title></b> @ <company>

   ⏳ Analyzing job requirements..."
   ```

3. Read `data/master-profile.json` to get the full master profile

4. **AI tailoring** (this is the intelligent part openclaw does):

   **CRITICAL: The master-profile.json contains REAL data. Every company name, job title, date range, education entry, and certification is factual and must be preserved EXACTLY. You are tailoring, NOT rewriting.**

   What you MUST keep unchanged (copy verbatim from master profile):
   - All `company` names exactly as written
   - All `title` values exactly as written
   - All `dates` and `location` values exactly as written
   - All `education` entries exactly as written
   - All `certifications` exactly as written
   - The `name`, `email`, `phone`, `linkedin` fields exactly as written
   - The number of experience entries (keep ALL of them, never drop any)

   What you CAN adjust (minor refinements only):
   - **Skills ordering**: reorder the skill categories so the most relevant one for this job appears first
   - **Summary paragraph**: rewrite to emphasize aspects relevant to this job, but keep it grounded in the real experience from the master profile
   - **Experience bullets**: reword existing bullets to emphasize relevant keywords, but the core facts (what was built, what tech was used, what results were achieved) must stay truthful
   - **Experience order**: optionally reorder experience entries to lead with the most relevant one

   What you MUST NOT do:
   - Do NOT invent new companies, roles, or experiences
   - Do NOT change dates, titles, company names, or locations
   - Do NOT add skills or certifications not in the master profile
   - Do NOT remove any experience entries or education
   - Do NOT change the person's name, contact info, or education history

5. Write tailored resume JSON to a temp file (same structure as master-profile.json, but with reordered/adjusted content). **Start by copying the master profile JSON, then make only the adjustments above.**

6. Render resume PDF:
   ```bash
   python3 render_pdf.py resume <tailored_resume.json> data/Resume_<Candidate>_<Company>.pdf
   ```

7. Write cover letter JSON to a temp file with this structure:
   ```json
   {
     "name": "<Candidate Name>",
     "contact": "<candidate.email@example.com> | <candidate phone>",
     "date": "<today's date>",
     "recipient": "Hiring Manager, <Company>",
     "subject": "Application for <Job Title>",
     "paragraphs": ["Dear Hiring Manager,\n\n...", "...", "Sincerely,\n<Candidate Name>"]
   }
   ```

8. Render cover letter PDF:
   ```bash
   python3 render_pdf.py cover <cover_letter.json> data/CoverLetter_<Candidate>_<Company>.pdf
   ```

9. Send both PDFs:
   ```bash
   python3 scraper.py --send-doc data/Resume_<Candidate>_<Company>.pdf
   python3 scraper.py --send-doc data/CoverLetter_<Candidate>_<Company>.pdf
   ```

10. Send completion message:
    ```bash
    python3 scraper.py --send-msg "✅ Done! Here are your tailored documents:
    📄 Resume_<Candidate>_<Company>.pdf
    📄 CoverLetter_<Candidate>_<Company>.pdf

    Key adjustments made:
    - <list what was emphasized/reordered>
    - <which skills matched>
    - <what was highlighted in cover letter>

    Good luck! 🚀"
    ```

11. Mark job as interested:
    ```bash
    python3 scraper.py --mark-interested <job_id>
    ```

### When user asks "job stats" or "search status":
- Query SQLite database at `data/jobs.db`
- Report: total jobs found, new today, applied count,
  top matches pending review

## CLI Reference
```bash
# Normal scraping with direct notification (legacy/manual)
python3 scraper.py

# Collect candidates without notification (Hermes cron mode)
python3 scraper.py --collect-only

# Get job as JSON
python3 scraper.py --get-job <job_id>

# Send message via Telegram
python3 scraper.py --send-msg "<html message>"

# Send document via Telegram
python3 scraper.py --send-doc <file_path> [caption]

# Mark job as interested
python3 scraper.py --mark-interested <job_id>

# Render resume PDF
python3 render_pdf.py resume <input.json> <output.pdf>

# Render cover letter PDF
python3 render_pdf.py cover <input.json> <output.pdf>

# Inspect currently open LinkedIn/ATS page through Chromium CDP
python3 -m jobhunter_auto_apply.cli inspect --job-id <job_id>

# Upload/submit only after explicit user approval
python3 -m jobhunter_auto_apply.cli upload --job-id <job_id> --selector 'input[type=file]' --file <resume.pdf> --approved
python3 -m jobhunter_auto_apply.cli submit --job-id <job_id> --selector 'button[type=submit]' --approved
```

## File Locations
- Scraper: `scraper.py`
- PDF renderer: `render_pdf.py`
- Master profile: `data/master-profile.json` (local, ignored)
- Profile schema example: `data/master-profile.example.json`
- Database: `data/jobs.db` (local, ignored)
- Logs: `data/scraper.log` (local, ignored)
- Config: `.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- Auto-apply engine: `jobhunter_auto_apply/`
- Dependencies: `requirements.txt`

## Cron Setup (Raspberry Pi)
```bash
# One-time setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Edit with real values

# Daily at 8 AM
0 8 * * * cd /home/pi/JobScrapper && .venv/bin/python3 scraper.py >> data/cron.log 2>&1
```

## Resume Tailoring Rules (MANDATORY)
These rules are NON-NEGOTIABLE. Violating them produces a fraudulent resume.
- **NEVER fabricate** companies, job titles, dates, education, certifications, or skills
- **NEVER change** company names, job titles, date ranges, locations, or education entries — copy them verbatim from master-profile.json
- **NEVER drop** experience entries — all entries from the master profile must appear in the tailored version
- **ONLY adjust**: summary paragraph wording, skills category ordering, experience bullet emphasis/rewording, experience entry ordering
- **Bullet rewording** means highlighting relevant keywords that are already truthful — NOT inventing new accomplishments
- The tailored JSON must have the exact same structure as master-profile.json
- When in doubt, keep the original text unchanged
