#!/bin/bash
# Job Hunter Daily Runner
# Runs at 18:00 Tunisia time

cd /home/ala/JobHunter
source .venv/bin/activate

# Run the scraper
python3 scraper.py >> data/cron.log 2>&1

exit 0
