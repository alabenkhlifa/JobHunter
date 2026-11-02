"""Candidate-directed project structure changes within one review variant."""

import copy
import uuid

from resume_blocks import project_for_variant


def text(value, label, limit=120):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Enter {label} of up to {limit} characters.")
    return value.strip()


def context(state, identifier):
    catalog = state['catalog']
    variant = next(v for v in catalog['variants'] if v['id'] == state['selection']['variant_id'])
    if identifier not in variant['project_ids']:
        raise ValueError('Choose a project in the current resume.')
    project = project_for_variant(catalog, variant, identifier)
    return catalog, variant, project


def employer_projects(catalog, variant, company):
    return [project_for_variant(catalog, variant, identifier) for identifier in variant['project_ids']
            if project_for_variant(catalog, variant, identifier)['company'] == company]


def split(state, body):
    catalog, variant, selected = context(state, body.get('project_id'))
    name = text(body.get('name'), 'a project name')
    if selected.get('entry'):
        raise ValueError('This experience already contains project sections.')
    overrides = variant.setdefault('project_overrides', {})
    for project in employer_projects(catalog, variant, selected['company']):
        item = copy.deepcopy(project)
        if not item.get('entry'):
            label = name if item['id'] == selected['id'] else item['label']
            item['label'] = label
            item['entry'] = {'name': label, 'dates': 'Not specified',
                             'tech': item['role'].get('tech', ''), 'bullets': item['role'].pop('bullets')}
        elif item['role'].get('tech') and not item['entry'].get('tech'):
            item['entry']['tech'] = item['role']['tech']
        item['role'].pop('tech', None)
        item['confirmation'] = 'draft'
        overrides[item['id']] = item


def add(state, body):
    catalog, variant, selected = context(state, body.get('project_id'))
    if any(not project.get('entry') for project in employer_projects(catalog, variant, selected['company'])):
        raise ValueError('Organize this employer into named projects first.')
    name = text(body.get('name'), 'a project name')
    bullet = text(body.get('bullet'), 'a first project bullet', 16000)
    tech = body.get('tech', '')
    if not isinstance(tech, str) or len(tech) > 2000:
        raise ValueError('Enter technology keywords of up to 2,000 characters.')
    identifier = 'project-' + uuid.uuid4().hex[:16]
    project = {'id': identifier, 'label': name, 'confirmation': 'draft', 'company': selected['company'],
               'role': {key: copy.deepcopy(value) for key, value in selected['role'].items() if key != 'bullets'},
               'entry': {'name': name, 'dates': 'Not specified', 'tech': tech.strip(), 'bullets': [bullet]}}
    catalog['projects'].append(project)
    variant.setdefault('project_overrides', {})[identifier] = copy.deepcopy(project)
    variant['project_ids'].append(identifier)
    for ids in (variant['default_project_ids'], state['selection']['project_ids']):
        positions = [index for index, current in enumerate(ids)
                     if project_for_variant(catalog, variant, current)['company'] == selected['company']]
        ids.insert(positions[-1] + 1 if positions else len(ids), identifier)


def remove(state, body):
    _, variant, project = context(state, body.get('project_id'))
    if len(variant['project_ids']) <= 1:
        raise ValueError('Keep at least one project in this resume.')
    identifier = project['id']
    variant['project_ids'].remove(identifier)
    variant['default_project_ids'] = [value for value in variant['default_project_ids'] if value != identifier]
    state['selection']['project_ids'] = [value for value in state['selection']['project_ids'] if value != identifier]
    if not variant['default_project_ids']:
        variant['default_project_ids'] = [variant['project_ids'][0]]
    if not state['selection']['project_ids']:
        state['selection']['project_ids'] = variant['default_project_ids'][:]
    variant.get('project_overrides', {}).pop(identifier, None)
    # The original catalog entry stays available to other variants and undo.
