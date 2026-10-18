"""Check nonterminal tracker offers, synchronize closures and send queued alerts."""

from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import jobhunter_availability as availability
from . import google_tracker as tracker
from . import google_tracker_sync as sync
from . import tracker_closures as closures

TERMINAL = {'closed', 'rejected'}


def read_tracker(args, sheets):
    metadata = sheets.spreadsheets().get(spreadsheetId=args.spreadsheet_id).execute()
    tabs = [s['properties'] for s in metadata.get('sheets', []) if (
        s['properties']['sheetId'] == args.sheet_id if args.sheet_id is not None else s['properties']['title'] == args.tab_name
    )]
    if len(tabs) != 1:
        raise ValueError('Configured tracker tab was not found.')
    quoted = "'" + tabs[0]['title'].replace("'", "''") + "'"
    values = sheets.spreadsheets().values().get(
        spreadsheetId=args.spreadsheet_id, range=quoted+'!A:N', valueRenderOption='FORMULA',
    ).execute().get('values', [])
    if not values or values[0] != tracker.HEADERS:
        raise ValueError('Tracker headers do not match the expected 14 columns.')
    return [list(values[0])] + [sync.normalized_row(row) for row in values[1:]]


def active_rows(rows):
    unique = {}
    for row in rows[1:]:
        if str(row[2]).strip().casefold() in TERMINAL or not any(row):
            continue
        if not sync.row_key(row)[0] or not row[3] or not row[4]:
            continue
        identity = closures.serialized_key(row, sync.row_key)
        # Prefer an already-submitted row when a sheet also retains its package.
        if identity not in unique or str(row[2]).casefold() == 'submitted':
            unique[identity] = row
    return unique


def match_job(conn, row):
    url = sync.row_key(row)[0]
    identity = availability._url_identity(url)
    matches = []
    for candidate in conn.execute('SELECT id,title,company,url,source,status FROM jobs'):
        job = dict(candidate)
        same_url = job['url'] == url or identity is not None and availability._url_identity(job['url']) == identity
        if same_url and availability._same_text(job['title'], row[3]) and availability._same_text(job['company'], row[4]):
            matches.append(job)
    if len(matches) > 1:
        raise ValueError('Multiple stored jobs match a tracker offer.')
    if matches:
        return matches[0], True
    return {'id': 'tracker-'+hashlib.sha256(repr(sync.row_key(row)).encode()).hexdigest()[:20],
            'title': row[3], 'company': row[4], 'url': url, 'source': ''}, False


def recorded_closure(conn, job):
    """Bootstrap only previously audited closures, never unavailable alone."""
    if job.get('status') != 'unavailable':
        return None
    saved = conn.execute('SELECT * FROM job_availability WHERE job_id=?', (job['id'],)).fetchone() if conn.execute("SELECT 1 FROM sqlite_master WHERE name='job_availability'").fetchone() else None
    if saved and saved['state'] == 'closed' and saved['matched'] and saved['reason'] in ('closed_marker','expired_valid_through'):
        return {'state':'closed', 'matched':True, 'reason':saved['reason'], 'checked_at':saved['checked_at']}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='job_feedback'").fetchone():
        feedback = conn.execute("SELECT created_at FROM job_feedback WHERE job_id=? AND action='unavailable' AND source='owner_chat' ORDER BY id DESC LIMIT 1", (job['id'],)).fetchone()
        if feedback:
            return {'state':'closed','matched':True,'reason':'owner_reported_closed','checked_at':feedback[0]}
    return None


def alert(event):
    def escaped(value, limit=220):
        return html.escape(str(value)[:limit], quote=True)
    return ('<b>Job offer closed</b>\n'
            +escaped(event['company'])+' — '+escaped(event['title'])+'\n'
            +'Previous tracker status: '+escaped(event['previous_status'],80)+'\n'
            +'The tracker now shows <b>closed</b>. Your application history is preserved.\n'
            +'<a href="'+escaped(event['url'],2048)+'">View offer</a>')


def validate_scope(args):
    root = getattr(args, 'candidate_root', None)
    if root is not None:
        root = Path(root).expanduser().resolve()
        if not args.account or any(not Path(path).expanduser().resolve().is_relative_to(root) for path in (args.db_path,args.google_token,args.drive_state)):
            raise ValueError('Monitor paths and account must belong to this candidate.')


def run_monitor(args, send, *, check=availability.check, synchronize=tracker.sync_tracker):
    validate_scope(args)
    state_dir = args.drive_state.parent
    state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd = os.open(state_dir/'listing_monitor.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return {'skipped_overlap':True}
        sheets,_ = tracker.google_services(args.google_token,args.account)
        rows = read_tracker(args,sheets)
        selected = active_rows(rows)
        db_uri = Path(args.db_path).resolve().as_uri()
        conn = sqlite3.connect(db_uri+'?mode='+('ro' if args.dry_run else 'rw'),uri=True,timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            work = []
            for identity,row in selected.items():
                job,stored = match_job(conn,row)
                known = recorded_closure(conn,job) if getattr(args,'use_recorded_closures',False) and stored else None
                work.append((identity,row,job,stored,known))
            def inspect(item):
                identity,row,job,stored,known = item
                return (*item,known or check(job))
            with ThreadPoolExecutor(max_workers=3) as pool:
                checked = list(pool.map(inspect,work))
            # A manual rejection/closure or deletion during network checks wins.
            current = active_rows(read_tracker(args,sheets))
            summary = {'checked':len(work),'open':0,'closed':0,'unknown':0,'new_closures':0,'notifications':0,'dry_run':bool(args.dry_run)}
            for identity,row,job,stored,known,result in checked:
                if identity not in current:
                    continue
                state = result['state']
                if state == 'closed' and (not result.get('matched') or result.get('reason') not in ('closed_marker','expired_valid_through','owner_reported_closed')):
                    state = 'unknown'
                summary[state] += 1
                if args.dry_run:
                    continue
                with conn:
                    if stored and known is None:
                        availability.recordcheck(conn,job,result)
                    if state == 'closed':
                        summary['new_closures'] += closures.record_closure(conn,current[identity],key=sync.row_key,
                            job_id=job['id'] if stored else None,checked_at=result['checked_at'],reason=result['reason'])
            if args.dry_run:
                return summary
            synchronize(args)  # Includes projection and exact read-back verification.
            visible = read_tracker(args,sheets)
            now = datetime.now(timezone.utc).isoformat()
            closures.mark_synced(conn,visible,key=sync.row_key,now=now)
            for event in closures.events(conn):
                if event['tracker_synced_at'] and not event['telegram_notified_at'] and not event['notification_cancelled_at']:
                    if not send(alert(event)):
                        raise RuntimeError('Telegram delivery unconfirmed; closure alert remains queued.')
                    with conn:
                        conn.execute('UPDATE tracker_listing_closures SET telegram_notified_at=? WHERE row_key=?', (datetime.now(timezone.utc).isoformat(),event['row_key']))
                    summary['notifications'] += 1
            return summary
        finally:
            conn.close()


def main(argv=None):
    import sys
    from dotenv import load_dotenv
    if not os.getenv('JOBHUNTER_CANDIDATE_ROOT'):
        load_dotenv(tracker.default_repo_root()/'.env')
    arguments = list(sys.argv[1:] if argv is None else argv)
    use_recorded = '--use-recorded-closures' in arguments
    if use_recorded:
        arguments.remove('--use-recorded-closures')
    args = tracker.parse_args(arguments)
    args.use_recorded_closures = use_recorded
    token,chat = os.getenv('TELEGRAM_BOT_TOKEN'),os.getenv('TELEGRAM_CHAT_ID')
    if not args.dry_run and (not token or not chat):
        print('Listing monitor failed: Telegram configuration is missing.')
        return 1
    from scraper import send_telegram
    try:
        print(json.dumps(run_monitor(args,lambda message:send_telegram(token,chat,message)),sort_keys=True))
        return 0
    except Exception:
        # Provider exception strings can contain credentials and private URLs.
        message='JobHunter listing check failed. Pending closure updates and alerts will retry at the next scheduled check.'
        print(message)
        if not args.dry_run and token and chat:
            send_telegram(token,chat,message)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
