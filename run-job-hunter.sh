#!/bin/bash
# Job Hunter Daily Runner

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source .venv/bin/activate

# Run the scraper
python3 scraper.py >> data/cron.log 2>&1

exit 0
