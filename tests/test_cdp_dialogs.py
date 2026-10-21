"""A dialog must not deadlock a command, leak its text, or trigger a retry."""
import json
import socket

import pytest

from jobhunter_auto_apply import cdp


URL = 'https://careers.clarivate.com/apply?jobSeqNo=CLACLAGBJREQ123EXTERNALENGB'


def client_with_frames(monkeypatch, frames, *, enabled=True):
    client = object.__new__(cdp.CDPClient)
    client.timeout = 1
    client._next_id = 0
    client._page_enabled = enabled
    client.pending_dialog = None
    client.dialog_events = []
    client.frame_path = ()
    class Socket:
        def settimeout(self, value):
            self.timeout = value
    client.sock = Socket()
    sent = []
    incoming = iter(frames)
    monkeypatch.setattr(cdp, '_send_frame', lambda sock, value: sent.append(json.loads(value)))
    def receive(sock):
        frame = next(incoming)
        if isinstance(frame, Exception):
            raise frame
        return json.dumps(frame)
    monkeypatch.setattr(cdp, '_recv_frame', receive)
    return client, sent


def dialog(message='Resume uploaded successfully', *, url=URL, kind='alert'):
    return {'method': 'Page.javascriptDialogOpening',
            'params': {'type': kind, 'url': url, 'message': message}}


@pytest.mark.parametrize('ack_first', [True, False])
def test_success_alert_during_command_preserves_original_response(monkeypatch, ack_first):
    response = {'id': 1, 'result': {'value': 42}}
    ack = {'id': 2, 'result': {}}
    client, sent = client_with_frames(monkeypatch, [dialog(), *( [ack, response] if ack_first else [response, ack])])
    assert client.call('Runtime.evaluate', {'expression': 'action()'}) == response
    assert [m['method'] for m in sent] == ['Runtime.evaluate', 'Page.handleJavaScriptDialog']
    assert sent[1]['params'] == {'accept': True}
    assert client.dialog_events == [{'type': 'alert', 'action': 'success'}]


def test_page_events_are_enabled_before_the_action(monkeypatch):
    client, sent = client_with_frames(monkeypatch, [{'id': 1, 'result': {}}, {'id': 2, 'result': {}}], enabled=False)
    client.call('Input.insertText', {'text': 'fixture'})
    assert [m['method'] for m in sent] == ['Page.enable', 'Input.insertText']


@pytest.mark.parametrize('message', ['Please upload resume', 'Please enter all required fields'])
def test_validation_alert_is_acknowledged_but_does_not_repeat_the_action(monkeypatch, message):
    client, sent = client_with_frames(monkeypatch, [dialog(message), {'id': 1, 'result': {}}, {'id': 2, 'result': {}}])
    with pytest.raises(cdp.BrowserDialogError) as exc:
        client.call('Runtime.evaluate', {'expression': 'next()'})
    assert exc.value.validation
    assert len(sent) == 2


@pytest.mark.parametrize('url,kind,message', [
    (URL, 'confirm', 'Resume uploaded successfully'),
    (URL, 'prompt', 'Resume uploaded successfully'),
    (URL, 'beforeunload', 'Resume uploaded successfully'),
    (URL, 'alert', 'Accept privacy terms and submit application'),
    (URL, 'alert', 'Resume uploaded successfully; agree to all terms'),
    ('https://careers.clarivate.com.example.test/apply', 'alert', 'Resume uploaded successfully'),
    ('http://careers.clarivate.com/apply', 'alert', 'Resume uploaded successfully'),
    ('https://other@careers.clarivate.com/apply', 'alert', 'Resume uploaded successfully'),
])
def test_unknown_or_wrong_origin_dialog_is_left_for_review(monkeypatch, url, kind, message):
    client, sent = client_with_frames(monkeypatch, [dialog(message, url=url, kind=kind)])
    with pytest.raises(cdp.BrowserDialogError) as exc:
        client.call('Runtime.evaluate')
    assert message not in str(exc.value)
    assert [m['method'] for m in sent] == ['Runtime.evaluate']
    with pytest.raises(cdp.BrowserDialogError):
        client.call('Runtime.evaluate')
    assert len(sent) == 1


def test_timeout_is_structured_and_never_resends(monkeypatch):
    client, sent = client_with_frames(monkeypatch, [socket.timeout()])
    with pytest.raises(cdp.BrowserTimeoutError, match='not retried'):
        client.call('Runtime.evaluate', {'expression': 'submit()'})
    assert len(sent) == 1


def test_failed_dialog_acknowledgement_does_not_report_success(monkeypatch):
    client, _ = client_with_frames(monkeypatch, [dialog(), {'id': 1, 'result': {}},
                                              {'id': 2, 'error': {'message': 'No dialog is showing'}}])
    with pytest.raises(cdp.BrowserDialogError):
        client.call('Runtime.evaluate')
