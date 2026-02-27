---
name: job-hunter
description: Automated job search agent for Gulf market (UAE/KSA).
  Scrapes LinkedIn and Foundit Gulf, scores matches against a
  Software Architect / Tech Lead profile, and notifies via Telegram.
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
Tech Lead roles in UAE and Saudi Arabia. It scrapes LinkedIn (guest API) and
Foundit Gulf (JSON API), scores matches using a weighted keyword system, and
sends notifications via Telegram.

## Architecture
- **Single file**: `scraper.py` (~850 lines)
- **Sources**: LinkedIn (guest HTML API), Foundit Gulf (JSON middleware API)
- **Storage**: SQLite for deduplication and history
- **Notifications**: Telegram Bot API (HTML parse mode)
- **Designed for**: Raspberry Pi via cron (lightweight, no headless browser)

## Scraping Strategy
The scraper uses **breadth-first round-robin** across 4 buckets:
- LinkedIn/UAE, LinkedIn/Saudi, Foundit/UAE, Foundit/Saudi
- Collects **3 matching jobs per bucket** (12 total)
- Fetches page 1 of every keyword before going to page 2
- Evaluates jobs after each page fetch to stop early
- Scrapers are generators that yield one page at a time

### Search Keywords
- software architect, cloud architect, tech lead
- lead software engineer, platform architect, solutions architect

### Regions
- **UAE**: searches "UAE" and "Dubai"
- **Saudi**: searches "Saudi Arabia" and "Riyadh"

## Scoring System
Jobs are scored by matching keywords in title + company + full description:
- **High (+3)**: architect, aws, azure, spring boot, microservices, tech lead,
  team lead, java, kotlin, backend
- **Medium (+1)**: docker, ci/cd, kubernetes, terraform, cloud, .net,
  typescript, devops, infrastructure
- **Threshold**: score >= 9 to qualify as a match

## Filters (applied before scoring)
1. **Excluded titles**: test engineer, qa, staff software engineer, sdet,
   machine learning, ml engineer, ml architect
2. **Job age**: posted within last 7 days only
3. **Local presence**: skips jobs requiring existing UAE/Saudi residency or
   that won't sponsor visas
4. **Experience**: skips jobs requiring more than 8 years

## Job Enrichment
For each candidate job, the scraper fetches the full description and extracts:
- **Tech stack**: split into required vs nice-to-have (parsed from section headers)
- **Min experience**: regex extraction from description
- **Salary**: regex extraction (AED/USD/SAR amounts)
- **Work model**: remote / hybrid / on-site (signal phrase matching)
- **Score breakdown**: lists each matched term with its weight

## Workflow

### When triggered by cron (scheduled):
1. Run the scraper: `.venv/bin/python3 scraper.py`
2. Scraper iterates all 4 buckets breadth-first
3. For each new job passing all filters with score >= 9:
   - Saves to SQLite database
   - Sends Telegram notification with:
     - Job title (bold), company, country
     - Work model badge, score with stars
     - Posted age, experience requirement, salary
     - Score breakdown (matched terms)
     - Tech stacks (required vs nice-to-have)
     - Clickable "View Job" link and source
4. Messages are batched under 4000 chars, sorted by score descending

### When user replies "interested":
1. Fetch the full job description from the URL
2. Read master profile from `data/master-profile.json`
3. Generate a tailored resume:
   - Reorder and emphasize skills matching the job description
   - Adjust professional summary to mirror job requirements
   - Highlight most relevant projects and experience
   - Keep all factual content accurate (never fabricate)
4. Generate a tailored cover letter:
   - Address company and role specifically
   - Connect experience to their requirements
   - Express relocation readiness
5. Save both as PDF files
6. Send both PDFs to user via Telegram

### When user asks "job stats" or "search status":
- Query SQLite database at `data/jobs.db`
- Report: total jobs found, new today, applied count,
  top matches pending review

## File Locations
- Scraper: `scraper.py`
- Database: `data/jobs.db`
- Logs: `data/scraper.log`
- Config: `.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- Dependencies: `requirements.txt` (requests, beautifulsoup4, python-dotenv, lxml)

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

## Resume Tailoring Rules
- NEVER fabricate experience, certifications, or skills
- ONLY reorder, emphasize, and adjust wording based on job requirements
- Always keep: all certifications, all experience entries, education
- Adjust: summary paragraph, skills ordering, bullet point emphasis
- Add: relevant keywords from the job description where truthful
