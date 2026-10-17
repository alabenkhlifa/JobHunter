import sys
import types

import pytest

from jobhunter_autopilot import messages
from test_autopilot_helpers import research_payload

JOB = {"id": "li-1", "title": "Senior Backend Engineer", "company": "Acme Software",
       "location": "Madrid, Spain", "url": "https://www.linkedin.com/jobs/view/1"}
CRITERIA = ["score 82 ≥ 75", "reviewer: send", "role: Senior Software Engineer", "location: Madrid, Spain"]


@pytest.fixture
def no_research_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "jobhunter_research", None)


def test_live_message_follows_the_template(no_research_module):
    text = messages.per_offer(JOB, mode="live", criteria=CRITERIA, research=research_payload("li-1"),
                              tracker_synced=True, evidence_attached=True)
    lines = text.splitlines()
    assert lines[0] == "✅ Auto-applied (live) · <b>Senior Backend Engineer — Acme Software</b> (Madrid)"
    assert lines[1] == "Why: " + " · ".join(CRITERIA)
    assert lines[2].startswith("Expected salary: €52k–€66k/year (Market: Levels.fyi)")
    assert lines[2].endswith("Asked: €62,000")
    assert lines[3].startswith("Company: Acme builds payment software.")
    assert '<a href="https://acme.example">acme.example</a>' in lines[3] and "Fintech / 201-500" in lines[3]
    assert lines[4] == ('Tracker: row submitted ✔ · Evidence: screenshot attached · '
                        '<a href="https://www.linkedin.com/jobs/view/1">View offer</a>')


def test_evidence_proposal_line_sits_above_the_footer(no_research_module):
    proposal = {"added": ["ev-a", "ev-b"], "removed": ["ev-c"], "rounds": 1}
    lines = messages.per_offer(JOB, mode="live", criteria=CRITERIA, research=None, tracker_synced=True,
                               evidence_proposal=proposal).splitlines()
    assert lines[4] == "Resume: +ev-a, +ev-b · −ev-c (per-job evidence proposal)"
    assert lines[5].startswith("Tracker:")
    only_added = messages.per_offer(JOB, mode="rehearsal", criteria=CRITERIA,
                                    evidence_proposal={"added": ["<ev>"], "removed": [], "rounds": 0})
    assert "Resume: +&lt;ev&gt; (per-job evidence proposal)" in only_added
    for empty in (None, {"added": [], "removed": [], "rounds": 0}):
        text = messages.per_offer(JOB, mode="live", criteria=CRITERIA, evidence_proposal=empty)
        assert "Resume:" not in text and len(text.splitlines()) == 5


def test_rehearsal_variant_and_missing_research(no_research_module):
    text = messages.per_offer(JOB, mode="rehearsal", criteria=CRITERIA, research=None)
    assert text.startswith("🧪 Rehearsed (not submitted)")
    assert "Tracker: unchanged (rehearsal)" in text and "Expected salary: not researched" in text
    pending = messages.per_offer(JOB, mode="live", criteria=CRITERIA, research=None, tracker_synced=False)
    assert "Sheet sync pending" in pending


def test_uses_research_formatter_when_available(monkeypatch):
    module = types.ModuleType("jobhunter_research")
    module.format_salary_lines = lambda result: ["Market: €50k–€60k &lt;gross&gt; (Levels.fyi &amp; Talent)",
                                                 "Estimate: €55k", "Ask: €62,000/year (75th pct)"]
    monkeypatch.setitem(sys.modules, "jobhunter_research", module)
    text = messages.per_offer(JOB, mode="live", criteria=CRITERIA, research=research_payload("li-1"))
    assert ("Expected salary: Market: €50k–€60k &lt;gross&gt; (Levels.fyi &amp; Talent) · Estimate: €55k · "
            "Asked: €62,000") in text
    assert "75th pct" not in text and "&amp;amp;" not in text


def test_hostile_values_are_truncated_then_escaped(no_research_module):
    hostile = dict(JOB, title="<script>" + "A" * 500 + "&amp;", company='"Evil" & <Co>',
                   url="javascript:alert(1)")
    research = research_payload("li-1")
    research["company"]["summary"] = "<b>" * 2000
    text = messages.per_offer(hostile, mode="live", criteria=["<i>" * 100] * 50, research=research)
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "&quot;Evil&quot; &amp; &lt;Co&gt;" in text
    assert "javascript:" not in text and "View offer" not in text
    assert len(text) <= messages.TELEGRAM_LIMIT
    # A cut never leaves a dangling entity.
    assert not any(part.endswith("&") or part.endswith("&am") for part in text.split("…"))


def test_fit_drops_lines_not_tags():
    lines = ["<b>head</b>"] + [f"<b>{'x' * 100}</b>" for _ in range(100)]
    text = messages.fit(lines)
    assert len(text) <= 4096 and text.endswith("…") and text.count("<b>") == text.count("</b>")


def test_summary_message():
    stats = {"run_id": "2026-10-15", "mode": "live", "applied": 1, "planned": 3,
             "rehearsed": [], "unverified": [{"title": "U", "company": "C"}],
             "not_attempted": [],
             "handed_off": [{"title": f"T{i}", "company": "<C>", "reason": "captcha"} for i in range(12)],
             "closed": [{"title": "Gone", "company": "X"}], "deferred": [{"title": "D", "company": "Y"}],
             "cap_left": 2, "max_per_day": 3}
    text = messages.summary(stats)
    assert "Applied: 1/3" in text and "Handed off to you: 12" in text
    assert "the site showed a CAPTCHA" in text and "… and 2 more" in text and "&lt;C&gt;" in text
    assert "Closed offers: 1" in text and "Deferred to the next run: 1" in text
    assert "Cap left today: 2/3" in text
    rehearsal = messages.summary(dict(stats, mode="rehearsal", rehearsed=[{"title": "R", "company": "C"}]))
    assert "Applied" not in rehearsal and "Rehearsed: 1" in rehearsal
    assert "evidence proposal" not in text
    assert "Resumes with a per-job evidence proposal: 2" in messages.summary(dict(stats, evidence_proposals=2))


def test_handoff_text():
    text = messages.handoff(JOB, "unanswerable:Notice <period>", "required field")
    assert "Notice &lt;period&gt;" in text and "required field" in text
    after = messages.handoff(JOB, "receipt_unverified", after_submit=True)
    assert "already clicked" in after
    card = "🔎 <b>Research</b>\n" + "line\n" * 2000
    assert len(messages.with_card(text, card)) <= 4096


def test_handoff_hints_tell_him_how_to_unblock():
    signed_out = messages.handoff(JOB, "linkedin_signed_out")
    assert "not signed in to LinkedIn" in signed_out and "sign in over VNC" in signed_out
    assert "VNC" in messages.handoff(JOB, "captcha")
    after = messages.handoff(JOB, "captcha", after_submit=True)
    assert "application browser (VNC)" in after and "noVNC" not in after
    assert len(messages.handoff(JOB, "form_unverified").splitlines()) == 1


def test_manual_apply_card(no_research_module):
    url = "https://jobs.smartrecruiters.com/Acme/1?a=1&b=<2>"
    text = messages.manual_apply(JOB, host="smartrecruiters.com", apply_url=url, criteria=CRITERIA,
                                 research=research_payload("li-1"), documents=1)
    lines = text.splitlines()
    assert lines[0] == "✋ <b>Apply by hand</b> · <b>Senior Backend Engineer — Acme Software</b> (Madrid)"
    assert "smartrecruiters.com blocks automated browsers" in lines[1] and "resume is attached" in lines[1]
    assert lines[2] == ('Apply: <a href="https://jobs.smartrecruiters.com/Acme/1?a=1&amp;b=&lt;2&gt;">'
                        'jobs.smartrecruiters.com</a>')
    assert lines[3] == "Why: " + " · ".join(CRITERIA)
    assert lines[4].startswith("Expected salary: ") and lines[4].endswith("Ask: €62,000")
    missing = messages.manual_apply(JOB, host="smartr.me", apply_url="javascript:alert(1)", documents=0)
    assert "link unavailable" in missing and "could not be attached" in missing and "javascript" not in missing
    assert "cover letter" in messages.manual_apply(JOB, host="smartr.me", apply_url=url, documents=2)


def test_manual_apply_card_for_a_hard_stop_met_in_the_browser(no_research_module):
    url = "https://bwelcome.hr.bnpparibas/en_US/externalcareers/ApplicationMethods?jobId=105012"
    text = messages.manual_apply(JOB, code="multi_step_form", detail="<Select your resume>", apply_url=url,
                                 criteria=CRITERIA, research=research_payload("li-1"), documents=1)
    lines = text.splitlines()
    assert lines[1] == ("The autopilot stopped: the application is a multi-step form the autopilot cannot fill yet"
                        " — &lt;Select your resume&gt;. Submit it yourself. The tailored resume is attached below.")
    assert lines[2].endswith(">bwelcome.hr.bnpparibas</a>") and lines[3].startswith("Why: ")
    captcha = messages.manual_apply(JOB, code="captcha", apply_url=url).splitlines()
    assert captcha[1].startswith("The autopilot stopped: the site showed a CAPTCHA.")
    assert "VNC" in captcha[3]
    assert "blocks automated browsers" in messages.manual_apply(JOB, code="anti_bot_ats", apply_url=url)
    assert ".. Submit" not in messages.manual_apply(JOB, code="form_unverified", detail="No form.", apply_url=url)


def test_manual_keyboard():
    url = "https://jobs.smartrecruiters.com/Acme/1"
    assert messages.manual_keyboard("sj-1", url) == {"inline_keyboard": [[
        {"text": "🔗 Open application", "url": url}, {"text": "🚫 Ignore", "callback_data": "ignore:sj-1"}]]}
    assert messages.manual_keyboard("sj-1", "ftp://x") == {"inline_keyboard": [[
        {"text": "🚫 Ignore", "callback_data": "ignore:sj-1"}]]}


def test_formatter_without_ranges_falls_back_to_the_stored_range(monkeypatch):
    module = types.ModuleType("jobhunter_research")
    module.format_salary_lines = lambda result: ["Ask: €62,000/year (75th pct)"]
    monkeypatch.setitem(sys.modules, "jobhunter_research", module)
    text = messages.per_offer(JOB, mode="live", criteria=CRITERIA, research=research_payload("li-1"))
    assert "Expected salary: €52k–€66k/year (Market: Levels.fyi) · Asked: €62,000" in text
