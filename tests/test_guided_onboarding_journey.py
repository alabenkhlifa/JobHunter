"""A beginner journey across durable Telegram ingress, with external APIs replaced."""
from unittest.mock import Mock

from jobhunter_service.dispatch import ApplicationTelegramHandler
from jobhunter_service.hermes import RestrictedHermesAssistant
from jobhunter_service.service import JobHunterService
from jobhunter_service.telegram_ingress import TelegramIngress


def test_beginner_configures_each_step_resumes_after_restart_and_recovers_from_model_outage(tmp_path):
    client = Mock()
    client.send_message.return_value = {'message_id': 1}
    client.download_document.return_value = b'Example Candidate\nEngineer, Example Co, 2022-2025\nBuilt APIs.'
    planner = Mock(return_value={'operation': 'reply', 'reply': 'What did you build in your first role?'})

    def start_runtime():
        service = JobHunterService(tmp_path, 900, telegram_client=client, public_url='https://jobs.example.test')
        handler = ApplicationTelegramHandler(service, client, RestrictedHermesAssistant(planner))
        return service, TelegramIngress(service, handler)

    service, ingress = start_runtime()
    for actor in (11, 22):
        service.admin(900, 'add', actor)
    service.authorize(22)
    untouched = service.store.member(22)
    sequence = 0

    def send(text='', *, document=None, callback=None):
        nonlocal sequence
        sequence += 1
        message = {'message_id': sequence, 'from': {'id': 11, 'is_bot': False}, 'chat': {'id': 11, 'type': 'private'}}
        if document:
            message['document'] = document
        else:
            message['text'] = text
        if callback:
            message['from'] = {'id': 999, 'is_bot': True}
            update = {'update_id': sequence, 'callback_query': {'id': 'test-' + str(sequence),
                'from': {'id': 11, 'is_bot': False}, 'message': message, 'data': callback}}
        else:
            update = {'update_id': sequence, 'message': message}
        ingress.enqueue(update)
        assert ingress.drain_one()
        with service.store.connect() as db:
            assert db.execute('SELECT status FROM telegram_ingress WHERE update_id=?', (sequence,)).fetchone()[0] == 'done'

    def click(prefix):
        button = next(button for call in reversed(client.send_message.call_args_list) if len(call.args) == 3
                      for row in call.args[2]['inline_keyboard'] for button in row
                      if button.get('callback_data', '').startswith(prefix))
        send(callback=button['callback_data'])

    def step():
        return service.onboarding_status(11)['next_step']

    def propose(text, patch):
        planner.side_effect = None
        planner.return_value = {'operation': 'propose', 'patch': patch}
        send(text)
        planner.return_value = {'operation': 'reply', 'reply': 'What else should we review about your experience?'}
        click('jh:confirm:')

    send('/start')
    assert step() == 'resume'
    send(document={'file_id': 'synthetic-file', 'file_name': 'resume.txt', 'mime_type': 'text/plain'})
    assert not service.snapshot(11)['settings']['resume']
    propose('I built APIs in that role. Use these exact facts.', {'resume': {
        'name': 'Example Candidate', 'certifications': ['Example certification'],
        'experience': [{'id': 'exp-1', 'title': 'Engineer', 'company': 'Example Co',
                        'dates': '2022-2025', 'bullets': ['Built APIs.']}]}})
    assert step() == 'resume'
    click('jh:onboard:acknowledge:resume:')
    assert step() == 'roles'

    service, ingress = start_runtime()
    send('/continue')
    assert step() == 'roles'
    assert service.snapshot(11)['settings']['resume']['experience'][0]['bullets'] == ['Built APIs.']
    planner.side_effect = RuntimeError('synthetic provider failure')
    send('Find backend engineer roles requiring two to five years.')
    assert any('temporarily unavailable' in call.args[1] for call in client.send_message.call_args_list)
    send('/status')
    assert step() == 'roles'
    planner.side_effect = None
    planner.return_value = {'operation': 'propose', 'patch': {'search': {
        'keywords': ['backend engineer'], 'matching': {'preferred_roles': ['backend engineer'],
            'seniority': {'min_years': 2, 'max_years': 5, 'preferred_min_years': 2, 'preferred_max_years': 5}}}}}
    send('/retry')
    assert not service.snapshot(11)['settings']['search']['keywords']
    click('jh:confirm:')
    click('jh:onboard:acknowledge:roles:')
    assert step() == 'markets'

    propose('Tunisia: authorized, no relocation. Germany: sponsorship and relocation required.', {'search': {'markets': [
        {'name': 'Tunisia', 'locations': ['Tunisia'], 'work_authorization': 'authorized', 'relocation_required': False},
        {'name': 'Germany', 'locations': ['Germany'], 'work_authorization': 'sponsorship_required', 'relocation_required': True}]}})
    click('jh:onboard:acknowledge:markets:')
    assert step() == 'schedule'
    send('/schedule 20:00 Africa/Tunis weekdays')
    click('jh:confirm:')
    assert service.snapshot(11)['settings']['schedule']['enabled'] is False
    click('jh:onboard:acknowledge:schedule:')
    assert step() == 'delivery'

    propose('Keep this private chat. Use compact messages grouped by destination.', {'telegram': {
        'presentation': {'style': 'compact', 'group_by_market': True}}})
    click('jh:onboard:acknowledge:delivery:')
    assert step() == 'linkedin'
    click('jh:onboard:skip:linkedin:')
    assert step() == 'gmail'
    send('/connect gmail')
    assert any('Google connection' in call.args[1] and 'skip' in call.args[1].lower()
               for call in client.send_message.call_args_list)
    send('/continue')
    click('jh:onboard:skip:gmail:')
    click('jh:onboard:skip:tracker:')
    assert step() == 'review'
    click('jh:onboard:acknowledge:review:')
    click('jh:onboard:activate:review:')
    assert not service.snapshot(11)['settings']['schedule']['enabled']
    click('jh:confirm:')
    snapshot = service.snapshot(11)
    assert snapshot['onboarding']['complete']
    assert snapshot['settings']['schedule']['enabled'] and snapshot['next_run']
    assert snapshot['settings']['telegram']['presentation']['style'] == 'compact'
    assert service.store.member(22) == untouched
    assert all(call.args[0] == 11 for call in client.send_message.call_args_list)
    assert client.download_document.call_count == 1
    assert not service.store.checkpoint('connection-check:11:linkedin')
    with service.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0] == 0
