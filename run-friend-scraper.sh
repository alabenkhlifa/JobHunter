#!/bin/bash
# Job Hunter runner for a secondary/local profile.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="${JOBHUNTER_PROFILE:-friend}"

cd "$SCRIPT_DIR"
source .venv/bin/activate

# Run scraper with the configured local profile.
mkdir -p "data/$PROFILE"
python3 scraper.py --profile "$PROFILE" >> "data/$PROFILE/scraper.log" 2>&1

exit 0
