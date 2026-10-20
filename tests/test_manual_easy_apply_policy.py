"""Keep operator instructions explicit about manual versus unattended Easy Apply."""
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("document", ["AGENTS.md", "job-hunter.skill.md"])
def test_manual_easy_apply_instructions_preserve_approval_and_safety_boundaries(document):
    text = (ROOT / document).read_text(encoding="utf-8")
    section = text.split("### Owner-approved manual LinkedIn Easy Apply\n", 1)
    assert len(section) == 2, "Document the manual allowance separately from autopilot"
    manual = section[1].split("\n##", 1)[0]
    for boundary in (
        "Proceed to apply",
        "separate final-submit approval",
        "exact job ID, role and employer",
        "confirmed answers",
        "CAPTCHA",
        "permanent package",
        "--policy-run",
        "autopilot",
    ):
        assert boundary in manual, f"Missing manual Easy Apply boundary: {boundary}"
