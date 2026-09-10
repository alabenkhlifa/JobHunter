"""Durable, fact-bound acknowledgements for explicitly started guided setup."""
from __future__ import annotations

import copy
import hashlib
import json

from jobhunter_matching import validate_config
from .presentation import DEFAULT_PRESENTATION

STEPS = ('resume', 'roles', 'markets', 'schedule', 'delivery', 'linkedin', 'gmail', 'tracker', 'review')
OPTIONAL = {'linkedin', 'gmail', 'tracker'}
LABELS = dict(zip(STEPS, ('Resume and experience', 'Roles and experience bounds',
    'Destinations and work authorization', 'Schedule', 'Delivery and message format',
    'LinkedIn', 'Gmail monitoring', 'Application tracker', 'Final review')))


def key(actor):
    return f'onboarding:{actor}'


def load(db, actor):
    row = db.execute('SELECT value FROM checkpoints WHERE key=?', (key(actor),)).fetchone()
    if row is None:
        return None
    state = json.loads(row['value'])
    if (not isinstance(state, dict) or state.get('version') != 1
            or type(state.get('generation')) is not int or not 0 <= state['generation'] < 2**32
            or not isinstance(state.get('acknowledgements'), dict)):
        raise ValueError('Saved onboarding progress needs operator review.')
    return state


def fresh():
    return {'version': 1, 'generation': 0, 'acknowledgements': {}, 'activated': None}


def save(db, actor, state):
    state['generation'] += 1
    if state['generation'] >= 2**32:
        raise ValueError('Saved onboarding progress needs operator review.')
    db.execute('INSERT INTO checkpoints VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key(actor), json.dumps(state)))


def revision(member_revision, state):
    return (member_revision << 32) + (state['generation'] if state else 0)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def subjects(settings, source_digest=''):
    search = validate_config(settings['search'])
    # The multi-user scheduler imposes its own smaller collection default.
    search['max_pages'] = settings['search'].get('max_pages', 2)
    return {
        'resume': {'profile': settings['resume'], 'source': source_digest},
        'roles': {name: search[name] for name in ('keywords', 'matching', 'score_threshold',
                  'max_job_age_days', 'max_pages', 'min_matching_jobs')},
        'markets': search['markets'],
        'schedule': {name: value for name, value in settings['schedule'].items() if name != 'enabled'},
        'delivery': {'limits': search['delivery'], 'destinations': settings['telegram']['destinations'],
                     'presentation': {**DEFAULT_PRESENTATION, **settings['telegram'].get('presentation', {})}},
        'linkedin': {'profile_link': settings['resume'].get('linkedin', '')},
        'gmail': settings['accounts']['gmail'],
        'tracker': settings['accounts']['tracker'],
    }


def binding(values, state):
    acknowledgements = state['acknowledgements'] if state else {}
    return digest({'subjects': values, 'choices': {
        step: acknowledgements.get(step) for step in STEPS if step != 'review'}})


def fingerprints(values, state):
    return {**{step: digest(value) for step, value in values.items()}, 'review': binding(values, state)}


def reconcile(state, before, after):
    if state is None:
        return None
    state = copy.deepcopy(state)
    changed = {step for step in before if digest(before[step]) != digest(after[step])}
    if changed:
        for step in changed | {'review'}:
            state['acknowledgements'].pop(step, None)
        state['activated'] = None
    return state


def acknowledge(state, values, step, mode='acknowledge'):
    state['acknowledgements'].pop('review', None)
    state['activated'] = None
    state['acknowledgements'][step] = {'fingerprint': fingerprints(values, state)[step], 'mode': mode}


def _line(value):
    return ' '.join(str(value or '').split())


def _list(values):
    return ', '.join(_line(value) for value in values) or 'None specified'


def descriptions(settings, values, connections):
    resume, roles = settings['resume'], values['roles']
    seniority = roles['matching']['seniority']
    bounds = '; '.join(f"{label}: {seniority.get(field) if seniority.get(field) is not None else 'no bound'}"
        for field, label in (('min_years', 'Minimum years'), ('max_years', 'Maximum years'),
                            ('preferred_min_years', 'Preferred minimum'), ('preferred_max_years', 'Preferred maximum')))
    market_lines = []
    authorization = {'authorized': 'authorized to work', 'sponsorship_required': 'employer sponsorship required',
                     'unknown': 'work authorization unknown; matches remain held'}
    for market in values['markets']:
        value = f"{_line(market['name'])}: {authorization[market['work_authorization']]}"
        value += '; relocation ' + ('required' if market['relocation_required'] else 'not required')
        if market.get('salary_target'):
            target = market['salary_target']
            value += f"; salary preference {target['amount']} {target['currency']}/{target['period']}"
        market_lines.append(value)
    schedule = settings['schedule']
    days = ', '.join(('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[day] for day in schedule['weekdays'])
    delivery = values['delivery']
    presentation = delivery['presentation']
    destination_names = [f"{_line(item.get('label') or item['kind'])} ({item['chat_id']})"
                         for item in delivery['destinations']]
    result = {
        'resume': f"{_line(resume.get('name')) or 'Name not confirmed'}; {len(resume.get('experience') or [])} experience entries. Only confirmed facts are used.",
        'roles': f"Search keywords: {_list(roles['keywords'])}. "
                 f"Preferred roles: {_list(roles['matching']['preferred_roles'])}. "
                 f"Excluded roles: {_list(roles['matching']['excluded_roles'])}. "
                 f"Preferred technologies: {_list(roles['matching']['preferred_technologies'])}. "
                 f"Excluded technologies: {_list(roles['matching']['excluded_technologies'])}. "
                 f"{bounds}. Excluded titles: {_list(seniority['excluded_titles'])}. "
                 f"Score threshold: {roles['score_threshold']}; maximum listing age: {roles['max_job_age_days']} days; "
                 f"collection pages per query: {roles['max_pages']}; minimum matching jobs: {roles['min_matching_jobs']}.",
        'markets': '\n'.join(market_lines) or 'No destinations confirmed. Travel visa exemptions do not establish work permission.',
        'schedule': f"{schedule['time']} {schedule['timezone']}; {days}; currently {'running' if schedule['enabled'] else 'paused'}.",
        'delivery': f"To: {_list(destination_names)}. Up to {delivery['limits']['per_market']} per market, {delivery['limits']['cap']} total. "
                    f"Format: {presentation['style']}; salary {'shown' if presentation['show_salary'] else 'hidden'}; "
                    f"match reasons {'shown' if presentation['show_match_reason'] else 'hidden'}; "
                    f"market grouping {'on' if presentation['group_by_market'] else 'off'}.",
    }
    for provider in OPTIONAL:
        status = connections.get(provider, {})
        result[provider] = _line(status.get('message')) or 'Connection has not been verified. You can skip this optional step.'
        if provider in {'gmail', 'tracker'}:
            account = settings['accounts'][provider]
            result[provider] += f" Account: {_line(account.get('account')) or 'not configured'}; integration {'enabled' if account['enabled'] else 'disabled'}."
            if provider == 'tracker' and account.get('viewer_email'):
                result[provider] += ' Personal viewer: ' + _line(account['viewer_email']) + '.'
    return result


def status(settings, state, member_revision, source_digest='', connections=None):
    connections = connections or {}
    values = subjects(settings, source_digest)
    state_for_read = state or fresh()
    prints = fingerprints(values, state_for_read)
    details = descriptions(settings, values, connections)
    required_ready = {'resume': bool(settings['resume'].get('name')),
                      'roles': bool(values['roles']['keywords']), 'markets': bool(values['markets']),
                      'schedule': True, 'delivery': bool(values['delivery']['destinations'])}
    questions = {
        'resume': 'Have we reviewed your experience and confirmed all resume facts you want to use? Confirm explicitly if you have no employment experience.',
        'roles': 'Confirm your search roles and the displayed experience limits, or request different supported minimum and maximum years.',
        'markets': 'Confirm each destination, your permission to work there, and whether you need employer sponsorship or relocation.',
        'schedule': 'Confirm the time, timezone and days for your job search. Fresh setup stays paused until final activation.',
        'delivery': 'Confirm your Telegram destinations, job limits and message format. You can keep the displayed defaults.',
        'linkedin': 'Connect and check your separate LinkedIn browser, or skip LinkedIn for now.',
        'gmail': 'Use a dedicated job application email for monitoring, or skip Gmail. Gmail monitoring is independent of your tracker.',
        'tracker': 'Connect the account that owns your application tracker and optionally share it with your personal account, or skip the tracker.',
        'review': 'Review the complete setup below, then acknowledge it before requesting an activation preview.',
    }
    if not required_ready['resume']:
        questions['resume'] = 'Upload your resume or share your background, then confirm the resume facts and review your experience one entry at a time.'
    if not required_ready['roles']:
        questions['roles'] = 'Which job roles should we search for? Then review the displayed minimum and maximum experience limits.'
        preferred = values['roles']['matching']['preferred_roles']
        if preferred:
            questions['roles'] = ('Your preferred roles are saved. Which search phrases should find those roles? '
                                  'You can confirm these as search keywords: ' + _list(preferred) + '.')
    steps = []
    for step in STEPS:
        entry = state_for_read['acknowledgements'].get(step, {})
        valid = bool(state and entry.get('fingerprint') == prints[step])
        connected = connections.get(step, {}).get('status') == 'connected'
        prerequisite = required_ready.get(step, connected if step in OPTIONAL else all(
            row['state'] in {'complete', 'skipped'} for row in steps))
        skipped = valid and entry.get('mode') == 'skip' and step in OPTIONAL
        complete = valid and entry.get('mode') == 'acknowledge' and (prerequisite or step in OPTIONAL)
        stage = 'skipped' if skipped else 'complete' if complete else 'pending' if prerequisite or step in OPTIONAL else 'blocked'
        actions = []
        if state:
            if stage in {'complete', 'skipped'}:
                actions.append('reopen')
            else:
                if prerequisite:
                    actions.append('acknowledge')
            if step in OPTIONAL:
                actions.extend(['check', 'skip'])
        steps.append({'id': step, 'label': LABELS[step], 'state': stage,
                      'question': questions[step], 'detail': details.get(step, ''), 'actions': actions})
    reviewed = bool(state and all(step['state'] in {'complete', 'skipped'} for step in steps))
    activated = bool(state and state.get('activated') == prints['review'])
    needs_check = [row for row in steps if row['id'] in OPTIONAL and row['state'] == 'complete'
                   and connections.get(row['id'], {}).get('status') != 'connected' and not activated]
    for row in needs_check:
        row['requires_check'] = True
        row['question'] = 'Check this previously acknowledged connection again before activation, or skip it for now.'
    eligible = reviewed and not needs_check
    complete = reviewed and activated
    if eligible:
        steps[-1]['actions'].append('activate')
    summary_lines = []
    for row in steps[:-1]:
        summary_lines.append(row['label'] + ': ' + row['detail'] + (' Skipped for now.' if row['state'] == 'skipped' else ''))
    summary = '\n\n'.join(summary_lines)
    steps[-1]['detail'] = summary
    next_stage = next((step for step in steps if step['state'] not in {'complete', 'skipped'}), None)
    if needs_check and (next_stage is None or next_stage['id'] == 'review'):
        next_stage = needs_check[0]
    next_step = next_stage['id'] if next_stage else None if complete else 'review'
    next_question = next_stage['question'] if next_stage else ('Guided setup is complete.' if complete else 'Your setup is reviewed. Request the activation preview and confirm it to enable scheduled searches.')
    return {'started': state is not None, 'revision': revision(member_revision, state),
            'settings_revision': member_revision, 'steps': steps, 'next_step': next_step,
            'next_question': next_question, 'ready_for_activation': eligible, 'complete': complete,
            'schedule_enabled': settings['schedule']['enabled'], 'summary': summary,
            'ready': eligible if state else all(required_ready[field] for field in ('resume', 'roles', 'markets')),
            'missing': [row['label'] for row in steps if row['state'] not in {'complete', 'skipped'}]
                       + [row['label'] + ' connection check' for row in needs_check]}
