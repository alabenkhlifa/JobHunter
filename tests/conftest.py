"""Keep tests independent of the deployment's enabled external integrations."""

import pytest


@pytest.fixture(autouse=True)
def disable_live_tracker_sync(monkeypatch):
    # Stage-recording tests must not launch the real command from the Pi's
    # .env. Tests of the hook can explicitly enable it and mock its command.
    monkeypatch.setenv("JOBHUNTER_AUTO_SYNC_TRACKER", "false")
    monkeypatch.setenv("JOBHUNTER_TRACKER_SHARE_WITH", "")
