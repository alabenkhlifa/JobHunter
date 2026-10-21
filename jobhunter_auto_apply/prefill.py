"""Audit an ATS's CV extraction against the permanent approved package.

Clarivate is the first supported adapter. Unknown facts and ambiguous rows
require review; no source document, profile, consent or legal answer is changed.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SNAPSHOT_JS = r"""(() => {
  if (location.protocol!=='https:' || location.hostname!=='careers.clarivate.com' ||
      location.pathname!=='/apply') return null;
  const shown=e=>!!e&&e.getBoundingClientRect().width>0&&getComputedStyle(e).visibility!=='hidden';
  const read=id=>{const e=document.getElementById(id);return !shown(e)?null:
    e.type==='checkbox'?e.checked:e.tagName==='SELECT'?e.selectedOptions[0]?.text.trim()||'':e.value;};
  const rows=(prefix,suffix)=>[...document.querySelectorAll('input[id]')]
    .filter(e=>new RegExp('^'+prefix+'\\[\\d+\\]\\.'+suffix+'$').test(e.id)&&shown(e))
    .map(e=>e.id.slice(0,e.id.lastIndexOf('.')));
  const work=rows('experienceData','companyName').map(p=>({prefix:p,company:read(p+'.companyName'),
    title:read(p+'.title'),start:read(p+'.fromTo.startDate'),end:read(p+'.fromTo.endDate'),
    current:read(p+'.fromTo.currentlyWorkHere'),description_length:(read(p+'.description')||'').length}));
  const education=rows('educationData','schoolName').map(p=>({prefix:p,school:read(p+'.schoolName'),
    degree:read(p+'.degree'),field:read(p+'.fieldOfStudy'),gpa:read(p+'.overallGPA')}));
  const phone=document.getElementById('phoneWidget.countryPhoneCode');
  return {adapter:'clarivate-v1',url:location.href,country:read('country'),
    phone_country:read('phoneWidget.countryPhoneCode'),
    phone_options:shown(phone)?[...phone.options].map(o=>o.text.trim()):[],
    previous_employee:read('isPreviousEmployee'),work,education,
    errors:[...document.querySelectorAll('.error,[role=alert]')].filter(shown)
      .map(e=>e.innerText.trim()).filter(Boolean).slice(0,10)};
})()"""


def _norm(value: Any) -> str:
    return " ".join(re.findall(r"\w+", str(value or "").casefold()))


def _period(text: str) -> tuple[str, str, bool] | None:
    months = {name: i for i, name in enumerate(
        ('january february march april may june july august september october november december').split(), 1)}
    tokens = re.findall(r"\d{1,2}/\d{4}|[A-Za-z]+\s+\d{4}", str(text))
    values = []
    for token in tokens:
        if '/' in token:
            month, year = token.split('/')
        else:
            name, year = token.split()
            month = months.get(name.casefold(), 0)
        if not 1 <= int(month) <= 12:
            return None
        values.append(f'{int(month):02d}/{year}')
    current = bool(re.search(r'\bpresent\b|\bcurrent\b', text, re.I))
    if len(values) == 1 and current:
        return values[0], '', True
    if len(values) == 2 and not current:
        return values[0], values[1], False
    return None


def _selector(field: str) -> str:
    return '[id=' + json.dumps(field) + ']'


def _school_sources(entry: dict, profile: dict) -> tuple[set[str], str]:
    school = str(entry.get('school') or '')
    aliases = {_norm(school)}
    degree = str(entry.get('degree') or '')
    for source in profile.get('education', []):
        source_school = str(source.get('school') or '')
        if (_norm(source.get('display_school')) == _norm(school)
                or _norm(re.split(r'\s+[—–-]\s+', source_school)[0]) == _norm(school)):
            aliases.add(_norm(source_school))
            degree += ' ' + str(source.get('degree') or '')
    level = ("Master's Degree" if re.search(r"\bmaster", degree, re.I) else
             "Bachelor's Degree" if re.search(r'\bbachelor', degree, re.I) else '')
    return aliases, level


def audit(snapshot: dict | None, resume: dict, profile: dict, scoped: dict | None = None) -> dict:
    result: dict[str, Any] = {'supported': False, 'issues': [], 'operations': [], 'manual_review': []}
    if not isinstance(snapshot, dict) or snapshot.get('adapter') != 'clarivate-v1':
        return result
    parsed = urlsplit(str(snapshot.get('url') or ''))
    if (parsed.scheme != 'https' or parsed.netloc != 'careers.clarivate.com' or parsed.path != '/apply'):
        return result
    result['supported'] = True
    issues, operations, manual = result['issues'], result['operations'], result['manual_review']

    def issue(code: str, op: dict | None = None) -> None:
        issues.append(code)
        if op:
            operations.append(op)
        else:
            manual.append(code)

    def set_field(code: str, field: str, expected: Any, actual: Any, *, kind: str = 'fill') -> None:
        if actual != expected:
            issue(code, {'kind': kind, 'selector': _selector(field), 'value': expected})

    defaults = profile.get('application_defaults') or {}
    location = str(defaults.get('current_work_location') or '')
    country = location.rsplit(',', 1)[-1].strip() if ',' in location else ''
    if snapshot.get('country') is not None:
        if country:
            set_field('country_changed_by_parser', 'country', country, snapshot['country'], kind='select')
        else:
            issue('confirmed_current_country_missing')
    if snapshot.get('phone_country') is not None:
        digits = re.sub(r'\D', '', str(profile.get('phone') or ''))
        matches = [label for label in snapshot.get('phone_options', [])
                   if label.startswith(country+' (+') and (m := re.search(r'\(\+(\d+)\)', label))
                   and digits.startswith(m[1])]
        if country and len(matches) == 1:
            set_field('phone_country_changed_by_parser', 'phoneWidget.countryPhoneCode', matches[0],
                      snapshot['phone_country'], kind='select')
        else:
            issue('confirmed_phone_country_ambiguous')
    previous = (scoped or {}).get('previous_clarivate_employee_or_contractor')
    if isinstance(previous, dict):
        previous = previous.get('answer', previous.get('value'))
    if isinstance(previous, str) and previous.casefold() in {'yes', 'no'}:
        previous = previous.casefold() == 'yes'
    if snapshot.get('previous_employee') is not None and isinstance(previous, bool):
        set_field('previous_employment_changed_by_parser', 'isPreviousEmployee',
                  'Yes' if previous else 'No', snapshot['previous_employee'], kind='select')

    expected = resume.get('experience') or []
    periods = [_period(str(r.get('dates') or '')) for r in expected]
    employers = {_norm(r.get('company')) for r in profile.get('experience', [])}
    client_names: set[str] = set()
    for role in profile.get('experience', []):
        for engagement in role.get('engagements', []):
            if isinstance(engagement, dict):
                for key in ('client', 'client_name', 'name', 'company', 'product', 'project'):
                    if isinstance(engagement.get(key), str):
                        client_names.add(_norm(engagement[key]))
    used: set[int] = set()
    for row in snapshot.get('work', []):
        prefix = str(row.get('prefix') or '')
        if not re.fullmatch(r'experienceData\[\d+\]', prefix):
            issue('work_control_identity_invalid'); continue
        company = _norm(row.get('company'))
        same_company = [i for i, e in enumerate(expected) if company and company == _norm(e.get('company'))]
        observed_period = (row.get('start') or '', '' if row.get('current') else row.get('end') or '', bool(row.get('current')))
        same_dates = [i for i, p in enumerate(periods) if p and p == observed_period]
        candidates = same_company or same_dates
        if len(candidates) != 1:
            issue('parsed_employer_requires_review'); continue
        index = candidates[0]
        if index in used:
            public_label = any(company and len(company) >= 5 and company in _norm(b)
                               for e in expected for b in e.get('bullets', []) if isinstance(b, str))
            if company not in employers and (company in client_names or public_label):
                number = int(re.search(r'\d+', prefix)[0])
                issue('client_parsed_as_duplicate_employer', {'kind': 'remove',
                    'selector': _selector(f'work-experience-array-button-remove-{number}')})
            else:
                issue('duplicate_work_entry_requires_review')
            continue
        used.add(index)
        source = expected[index]
        set_field('parsed_employer_mismatch', prefix+'.companyName', source['company'], row.get('company'))
        set_field('parsed_job_title_mismatch', prefix+'.title', source['title'], row.get('title'))
        if periods[index] and observed_period != periods[index]:
            issue('parsed_employment_dates_mismatch')
        if row.get('description_length', 0) > 2000:
            bullets: list[str] = []
            for bullet in source.get('bullets', []):
                if isinstance(bullet, str) and len('\n'.join(bullets+[bullet])) <= 2000:
                    bullets.append(bullet)
            if bullets:
                issue('parsed_description_exceeds_form_limit', {'kind': 'fill',
                    'selector': _selector(prefix+'.description'), 'value': '\n'.join(bullets)})
            else:
                issue('parsed_description_requires_review')
    if snapshot.get('work') and len(used) != len(expected):
        issue('approved_employment_entries_missing')

    expected_education = resume.get('education') or []
    for index, row in enumerate(snapshot.get('education', [])):
        prefix = str(row.get('prefix') or '')
        if not re.fullmatch(r'educationData\[\d+\]', prefix):
            issue('education_control_identity_invalid'); continue
        if index >= len(expected_education):
            if not any(row.get(k) for k in ('school', 'degree', 'field', 'gpa')):
                number = int(re.search(r'\d+', prefix)[0])
                issue('empty_parser_education_entry', {'kind': 'remove',
                    'selector': _selector(f'education-details-array-button-remove-{number}')})
            else:
                issue('extra_education_requires_review')
            continue
        source = expected_education[index]
        aliases, level = _school_sources(source, profile)
        if _norm(row.get('school')) not in aliases:
            if row.get('school'):
                issue('different_school_requires_review')
            else:
                set_field('parsed_school_missing', prefix+'.schoolName', source['school'], row.get('school'))
        if level:
            set_field('parsed_degree_mismatch', prefix+'.degree', level, row.get('degree'), kind='select')
        elif not row.get('degree'):
            issue('degree_level_requires_confirmation')
    if snapshot.get('education') and len(snapshot['education']) < len(expected_education):
        issue('approved_education_entries_missing')
    if snapshot.get('errors'):
        errors = _norm(' '.join(snapshot['errors'])).replace('please enter all required fields', '').strip()
        understood = ((errors == 'invalid phone number' and 'phone_country_changed_by_parser' in issues)
                      or (errors == 'max length allowed is 2000 characters' and
                          'parsed_description_exceeds_form_limit' in issues)
                      or (not errors and operations))
        if not understood:
            issue('site_validation_errors_visible')
    return result


def load_sources(db_path: str, job_id: str, profile_path: str) -> tuple[dict, dict, dict, str]:
    import sqlite3
    with sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True) as conn:
        row = conn.execute('SELECT package_path FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1',
                           (job_id,)).fetchone()
    if not row or not row[0] or not Path(row[0]).is_dir():
        raise ValueError('Permanent approved package is unavailable; do not regenerate it.')
    package = Path(row[0])
    raw = (package/'resume.json').read_bytes()
    manifest_path = package/'tailoring_manifest.json'
    if manifest_path.exists() and json.loads(manifest_path.read_text()).get('job_id') != job_id:
        raise ValueError('Approved package belongs to another application.')
    profile_file = Path(profile_path)
    profile = json.loads(profile_file.read_text()) if profile_file.is_file() else {}
    scoped_file = Path(db_path).parent/'application-answers'/f'{job_id}.json'
    scoped = json.loads(scoped_file.read_text()) if scoped_file.is_file() else {}
    if scoped and scoped.get('job_id') != job_id:
        raise ValueError('Scoped answers belong to another application.')
    signature = raw + json.dumps(profile, sort_keys=True).encode() + json.dumps(scoped, sort_keys=True).encode()
    return json.loads(raw), profile, scoped, hashlib.sha256(signature).hexdigest()
