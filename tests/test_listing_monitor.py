import argparse
import fcntl
import sqlite3
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from jobhunter_integrations import google_tracker as tracker
from jobhunter_integrations import google_tracker_sync as sync
from jobhunter_integrations import listing_monitor as monitor
from jobhunter_integrations import tracker_closures as closures

STAMP = '2026-10-15T15:00:00+00:00'


def row(job_id='123', stage='submitted'):
    return ['01/10/2026 10:00','01/10/2026 10:00',stage,'Senior Java Engineer','Example','LinkedIn',
            'https://www.linkedin.com/jobs/view/'+job_id,'https://ats.example/jobs/'+job_id,
            '=HYPERLINK("https://drive.google.com/resume", "Open resume")','','','',
            'Existing candidate note','Monitor recruiter replies']


@pytest.fixture
def database(tmp_path):
    path = tmp_path/'jobs.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE jobs(id TEXT PRIMARY KEY,title TEXT,company TEXT,url TEXT,source TEXT,status TEXT);
        CREATE TABLE applications(id INTEGER PRIMARY KEY,job_id TEXT,stage TEXT,submitted_at TEXT,notes TEXT);''')
        for jid,stage in [('123','submitted'),('456','interested'),('789','rejected')]:
            r=row(jid,stage)
            conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', ('li-'+jid,r[3],r[4],r[6],'linkedin',stage))
            conn.execute('INSERT INTO applications VALUES(?,?,?,?,?)',(int(jid),'li-'+jid,stage,STAMP if stage=='submitted' else None,'Evidence retained'))
    return path


def test_projection_preserves_submission_dates_documents_and_rejected_outcomes(database):
    original=row()
    with sqlite3.connect(database) as conn:
        history=list(conn.execute('SELECT * FROM applications'))
        assert closures.record_closure(conn,original,key=sync.row_key,job_id='li-123',checked_at=STAMP,reason='closed_marker')
        assert not closures.record_closure(conn,original,key=sync.row_key,job_id='li-123',checked_at=STAMP,reason='closed_marker')
        assert list(conn.execute('SELECT * FROM applications')) == history
    projected=closures.project_rows([tracker.HEADERS,original,row(stage='rejected')],database,key=sync.row_key)
    closed=projected[1]
    assert closed[2]=='closed'
    assert closed[0]==original[0] and closed[6:12]==original[6:12]
    assert 'application status: submitted' in closed[12]
    assert 'Existing candidate note' in closed[12]
    assert projected[2]==row(stage='rejected')
    assert closures.project_rows(projected,database,key=sync.row_key)==projected
    assert tracker.status_color('closed') != tracker.status_color('rejected')
    assert tracker.status_color('closed') != tracker.status_color('skipped')


def test_closure_survives_ordinary_tracker_merge_and_keeps_prior_stage(database):
    original=row()
    with sqlite3.connect(database) as conn:
        closures.record_closure(conn,original,key=sync.row_key,job_id='li-123',checked_at=STAMP,reason='closed_marker')
    projected=closures.project_rows([tracker.HEADERS,original],database,key=sync.row_key)
    merged,_=sync.merge_rows(projected,[original])
    assert closures.project_rows(merged,database,key=sync.row_key)==projected


@pytest.mark.parametrize('terminal', ['closed', 'rejected'])
@pytest.mark.parametrize('newer', [False, True])
def test_manual_terminal_sheet_status_cannot_be_reopened_by_normal_sync(terminal,newer):
    previous=row(stage=terminal)
    incoming=row(stage='submitted')
    if newer:
        incoming[1]='16/10/2026 12:00'
    after,_=sync.merge_rows([tracker.HEADERS,previous],[incoming])
    assert after[1][2]==terminal
    assert after[1][13]==previous[13]


def test_newer_recruiter_outcome_can_advance_a_previously_rejected_application():
    previous=row(stage='rejected')
    incoming=row(stage='interview_invited')
    incoming[1]='16/10/2026 12:00'
    after,_=sync.merge_rows([tracker.HEADERS,previous],[incoming])
    assert after[1][2]=='interview_invited'


def test_active_selection_includes_every_other_status_and_deduplicates_same_offer():
    values=[tracker.HEADERS,row('123','package_generated'),row('123','submitted'),row('456','interview_invited'),
            row('789',' Closed '),row('901','REJECTED'),row('902','custom_stage')]
    active=list(monitor.active_rows(values).values())
    assert [r[2] for r in active]==['submitted','interview_invited','custom_stage']


@pytest.fixture
def harness(database,monkeypatch,tmp_path):
    args=argparse.Namespace(db_path=database,repo_root=tmp_path,drive_state=tmp_path/'state/drive.json',
        google_token=tmp_path/'token.json',account=None,candidate_root=None,spreadsheet_id='test',
        sheet_id=1,tab_name='Applications',dry_run=False,use_recorded_closures=False)
    visible=[tracker.HEADERS,row(),row('456','interested'),row('789','rejected')]
    monkeypatch.setattr(tracker,'google_services',Mock(return_value=(Mock(),Mock())))
    monkeypatch.setattr(monitor,'read_tracker',lambda *_:[list(r) for r in visible])
    def synchronize(_):
        visible[:]=closures.project_rows(visible,database,key=sync.row_key)
        return {'verified_rows':len(visible)-1}
    def check(job):
        listing=monitor.availability._listing(job)
        return {'job_id':job['id'],'state':'closed','reason':'closed_marker','matched':True,'checked_at':STAMP,
                'url':listing.url,'source_job_id':listing.source_id}
    return args,visible,synchronize,Mock(side_effect=check)


def test_monitor_checks_submitted_and_interested_skips_rejected_and_notifies_once(harness,database):
    args,visible,synchronize,check=harness
    send=Mock(return_value=True)
    with sqlite3.connect(database) as conn:
        history=list(conn.execute('SELECT * FROM applications'))
    result=monitor.run_monitor(args,send,check=check,synchronize=synchronize)
    assert result['checked']==result['closed']==result['notifications']==2
    assert send.call_count==2
    assert all(r[2]=='closed' for r in visible[1:3])
    again=monitor.run_monitor(args,send,check=check,synchronize=synchronize)
    assert again['checked']==again['notifications']==0
    assert check.call_count==2 and send.call_count==2
    with sqlite3.connect(database) as conn:
        assert list(conn.execute('SELECT * FROM applications'))==history
        assert conn.execute("SELECT status FROM jobs WHERE id='li-789'").fetchone()[0]=='rejected'


@pytest.mark.parametrize('state,reason,matched',[('unknown','login_or_challenge',False),('unknown','request_failed',False),('open','application_control',True)])
def test_unknown_or_open_never_closes_or_notifies(harness,database,state,reason,matched):
    args,visible,synchronize,check=harness
    original=check.side_effect
    def checked(job):
        return dict(original(job),state=state,reason=reason,matched=matched)
    send=Mock()
    result=monitor.run_monitor(args,send,check=checked,synchronize=synchronize)
    assert result['closed']==0
    send.assert_not_called()
    with sqlite3.connect(database) as conn:
        assert closures.events(conn)==[]


def test_sync_failure_retains_outbox_and_never_sends_premature_success(harness,database):
    args,visible,synchronize,check=harness
    send=Mock(return_value=True)
    with pytest.raises(RuntimeError,match='sync failed'):
        monitor.run_monitor(args,send,check=check,synchronize=Mock(side_effect=RuntimeError('sync failed')))
    send.assert_not_called()
    with sqlite3.connect(database) as conn:
        assert len(closures.events(conn))==2
    assert monitor.run_monitor(args,send,check=check,synchronize=synchronize)['notifications']==2


def test_partial_telegram_failure_retries_only_unacknowledged_alert(harness):
    args,_,synchronize,check=harness
    send=Mock(side_effect=[True,False])
    with pytest.raises(RuntimeError,match='unconfirmed'):
        monitor.run_monitor(args,send,check=check,synchronize=synchronize)
    retry=Mock(return_value=True)
    assert monitor.run_monitor(args,retry,check=check,synchronize=synchronize)['notifications']==1
    assert retry.call_count==1


def test_sheet_rejection_during_checks_prevents_closure(harness,database):
    args,visible,synchronize,check=harness
    def inspect(job):
        visible[1][2]='rejected'
        visible[2][2]='rejected'
        return check(job)
    send=Mock()
    assert monitor.run_monitor(args,send,check=inspect,synchronize=synchronize)['new_closures']==0
    send.assert_not_called()


def test_manual_sheet_only_offer_is_checked_projected_and_not_added_as_application(harness,database):
    args,visible,synchronize,check=harness
    visible[:]=[tracker.HEADERS,row('999','interested')]
    send=Mock(return_value=True)
    assert monitor.run_monitor(args,send,check=check,synchronize=synchronize)['notifications']==1
    assert visible[1][2]=='closed'
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT count(*) FROM applications').fetchone()[0]==3


def test_dry_run_has_no_database_sheet_or_notification_changes(harness,database):
    args,visible,synchronize,check=harness
    args.dry_run=True
    send=Mock()
    synchronizer=Mock()
    before=database.read_bytes()
    result=monitor.run_monitor(args,send,check=check,synchronize=synchronizer)
    assert result['closed']==2
    assert database.read_bytes()==before
    send.assert_not_called()
    synchronizer.assert_not_called()


def test_overlapping_monitor_does_not_read_sheet_or_send(harness):
    args,_,synchronize,check=harness
    args.drive_state.parent.mkdir()
    with (args.drive_state.parent/'listing_monitor.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert monitor.run_monitor(args,Mock(),check=check,synchronize=synchronize)=={'skipped_overlap':True}
    check.assert_not_called()


def test_candidate_scope_rejects_owner_paths_before_io(harness):
    args,_,synchronize,check=harness
    args.candidate_root=args.repo_root/'another-candidate'
    args.account='candidate@example.com'
    with pytest.raises(ValueError,match='belong'):
        monitor.run_monitor(args,Mock(),check=check,synchronize=synchronize)
    assert not args.drive_state.parent.exists()


def test_alert_escapes_untrusted_titles_and_preserves_submitted_meaning():
    event={'company':'A&B','title':'<script>','previous_status':'submitted','url':'https://example.com/?a=1&b=2'}
    text=monitor.alert(event)
    assert 'A&amp;B' in text and '&lt;script&gt;' in text and '<script>' not in text
    assert 'submitted' in text and 'history is preserved' in text


def test_bootstrap_never_treats_unavailable_without_evidence_as_closed(database):
    with sqlite3.connect(database) as conn:
        conn.row_factory=sqlite3.Row
        assert monitor.recorded_closure(conn,{'id':'li-123','status':'unavailable'}) is None


def test_rejected_or_deleted_row_cancels_unsent_alert(database):
    with sqlite3.connect(database) as conn:
        closures.record_closure(conn,row(),key=sync.row_key,job_id='li-123',checked_at=STAMP,reason='closed_marker')
        closures.mark_synced(conn,[tracker.HEADERS,row(stage='rejected')],key=sync.row_key,now=STAMP)
        assert closures.events(conn)[0]['notification_cancelled_at']==STAMP
