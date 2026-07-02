#!/bin/bash
# Friend Job Hunter - Daily scraper for Ahmed's profile
# Runs at 18:00 daily

cd /home/ala/JobHunter
source .venv/bin/activate

# Run scraper with friend profile
python3 scraper.py --profile friend >> data/friend/scraper.log 2>&1

exit 0
