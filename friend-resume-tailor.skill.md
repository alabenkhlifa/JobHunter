---
name: friend-resume-tailor
description: Resume tailoring for friend's job applications via ntfy.
  Sandboxed to data/friend/ directory only.
triggers:
  - friend resume
  - friend job
  - tailor friend
---

# Friend Resume Tailor Skill

## Overview
This skill handles resume and cover letter tailoring for the friend's profile.
It is ISOLATED from the main user's profile and operates exclusively within
`data/friend/`.

## Scope (STRICTLY LIMITED)
This skill can ONLY:
- Read `data/friend/master-profile.json`
- Read `data/friend/jobs.db`
- Generate tailored resume + cover letter JSON
- Render PDFs via `render_pdf.py`
- Send files via ntfy to the friend's publish_topic
- Mark jobs as interested in `data/friend/jobs.db`

This skill MUST NOT:
- Access `data/master-profile.json` (main user's profile)
- Access `data/jobs.db` (main user's database)
- Access any directory outside `data/friend/`
- Execute arbitrary commands
- Modify scraper configuration

## Resume Tailoring Rules (MANDATORY)
Same rules as the main job-hunter skill:
- NEVER fabricate companies, job titles, dates, education, certifications, or skills
- NEVER change company names, job titles, date ranges, locations, or education
- NEVER drop experience entries
- ONLY adjust: summary wording, skills ordering, bullet emphasis, experience ordering
- The tailored JSON must have the exact same structure as master-profile.json

## Automated Flow (ntfy_listener.py)
The `ntfy_listener.py friend` process handles this automatically:
1. Friend sends a job ID (e.g., `li-12345`) to the listen_topic
2. Listener validates the ID, looks up the job, tailors resume via Anthropic API
3. Renders PDFs and sends them to the publish_topic
4. Marks job as interested

## Manual Flow
```bash
# Get job details
python3 scraper.py --profile friend --get-job <job_id>

# Send message to friend
python3 scraper.py --profile friend --send-msg "message"

# Send document to friend
python3 scraper.py --profile friend --send-doc <file_path>

# Mark interested
python3 scraper.py --profile friend --mark-interested <job_id>
```

## File Locations
- Config: `data/friend/config.json`
- Master profile: `data/friend/master-profile.json`
- Database: `data/friend/jobs.db`
- Logs: `data/friend/scraper.log` (scraper), `data/friend/listener.log` (listener)
- Output PDFs: `data/friend/output/`
