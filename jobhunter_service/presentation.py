"""Pure text presentation for job digests and existing escaped mail alerts."""
from html.parser import HTMLParser
from itertools import islice

from .job_intents import JOB_ID, MAX_DIGEST_JOBS, OPERATIONS


DEFAULT_PRESENTATION = {
    'style': 'standard',
    'show_salary': False,
    'show_match_reason': False,
    'group_by_market': False,
}
ACTION_HINT = ('Source listing checked; application form not yet verified. '
               'Choose Details, Interested or Prepare below, or say "details for job 1". '
               'Prepare creates your resume; upload and final submission need separate confirmation.')


def digest_markup(selected_ids, *, numbered=True):
    """Telegram-safe navigation only, bounded to the candidate digest limit."""
    rows = []
    seen = set()
    for number, job_id in enumerate(islice(selected_ids, MAX_DIGEST_JOBS), 1):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id) or job_id in seen:
            continue
        seen.add(job_id)
        row = []
        for operation in OPERATIONS:
            callback = f'jh:job:{operation}:{job_id}'
            if len(callback.encode('utf-8')) <= 64:
                label = operation.title()
                row.append({'text': f'{number} {label}' if numbered else label, 'callback_data': callback})
        if row:
            rows.append(row)
    return {'inline_keyboard': rows} if rows else None


def _options(value):
    # Settings are validated before persistence. A legacy/malformed optional
    # value still cannot enable features through truthy strings or templates.
    supplied = value if isinstance(value, dict) else {}
    return {
        'style': 'compact' if supplied.get('style') == 'compact' else 'standard',
        **{key: supplied.get(key) is True for key in DEFAULT_PRESENTATION if key != 'style'},
    }


def _optional_text(value, unknown, limit=360):
    if not isinstance(value, str):
        return unknown
    text = ' '.join(value.split())
    if not text:
        return unknown
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _job_text(job, options, number):
    heading = f"{number}. {job['title']} — {job['company']}"
    if options['style'] == 'compact':
        lines = [f"{heading} | {job['location']}"]
    else:
        lines = [heading, str(job['location'])]
    if options['show_salary']:
        # Only the listing's extracted salary is shown. A candidate's target
        # or market estimate must never be presented as advertised pay.
        lines.append('Salary: ' + _optional_text(job.get('salary'), 'Not listed'))
    if options['show_match_reason']:
        lines.append('Match reason: ' + _optional_text(job.get('ai_verdict_reason'), 'Not available'))
    # Keep the original source URL and actionable role ID intact in every style.
    lines.extend([str(job['url']), f"/details {job['id']}"])
    return '\n'.join(lines)


def render_digest(jobs, queued, *, presentation=None, markets=None):
    """Format an already-selected list without selecting or mutating any jobs."""
    jobs = list(jobs)
    options = _options(presentation)
    blocks = [f'Job matches: {len(jobs)} selected, {queued} queued']
    if options['group_by_market']:
        from jobhunter_matching import resolve_market
        groups = {}
        for number, job in enumerate(jobs, 1):
            market = resolve_market(job.get('location'), markets or [])
            label = _optional_text(market.get('name'), 'Other locations') if market else 'Other locations'
            # First appearance retains the earliest reviewed rank per market;
            # the original reviewed order is preserved inside every group.
            groups.setdefault(label, []).append((number, job))
        for label, members in groups.items():
            blocks.append(f'Market: {label}')
            blocks.extend(_job_text(job, options, number) for number, job in members)
    else:
        blocks.extend(_job_text(job, options, number) for number, job in enumerate(jobs, 1))
    blocks.append(ACTION_HINT)
    return '\n\n'.join(blocks)


def plain_html(value):
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
        def handle_data(self, data):
            self.parts.append(data)
    parser = Text()
    parser.feed(value)
    return ''.join(parser.parts)
