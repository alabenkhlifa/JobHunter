"""Configuration and receiver selection for shared owner integrations."""
import sys
import threading
from unittest.mock import Mock

import pytest

from jobhunter_service import cli


@pytest.fixture
def settings(tmp_path, monkeypatch):
    import os
    for name in list(os.environ):
        if name.startswith('JOBHUNTER_'):
            monkeypatch.delenv(name)
    values = {
        'JOBHUNTER_SERVICE_BOT_TOKEN': '123456789:synthetic-token-for-tests-only',
        'JOBHUNTER_OWNER_TELEGRAM_USER_ID': '900',
        'JOBHUNTER_SERVICE_DATA_ROOT': str(tmp_path / 'state'),
        'JOBHUNTER_ADMIN_TOKEN': 'x' * 40,
        'JOBHUNTER_HERMES_PYTHON': sys.executable,
        'JOBHUNTER_HERMES_SOURCE': str(tmp_path),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return tmp_path


def test_shared_login_does_not_require_separate_provider_or_key(settings, monkeypatch):
    from jobhunter_service.hermes_runner import SharedOwnerPlanner
    monkeypatch.setenv('JOBHUNTER_OWNER_AUTH_SOCKET', str(settings / 'auth.sock'))
    monkeypatch.setenv('JOBHUNTER_TELEGRAM_MODE', 'shared')
    service, client, planner = cli.configured_service()
    assert isinstance(planner, SharedOwnerPlanner)
    assert service.owner_id == 900
    # Config validation neither contacts the socket nor makes a model request.
    assert cli.main(['check']) == 0


def test_standalone_mode_still_requires_explicit_model_settings(settings):
    with pytest.raises(ValueError, match='JOBHUNTER_MODEL_API_KEY'):
        cli.configured_service()


def test_bad_receiver_mode_fails_before_initialization(settings, monkeypatch):
    monkeypatch.setenv('JOBHUNTER_TELEGRAM_MODE', 'both')
    configured = Mock()
    monkeypatch.setattr(cli, 'configured_service', configured)
    with pytest.raises(ValueError, match='shared or polling'):
        cli.serve()
    configured.assert_not_called()


@pytest.mark.parametrize('mode,receiver', [('shared', 'incoming'), ('polling', 'polling')])
def test_only_the_selected_telegram_receiver_is_started(settings, monkeypatch, mode, receiver):
    monkeypatch.setenv('JOBHUNTER_TELEGRAM_MODE', mode)
    (settings / 'service').mkdir()
    service = Mock(root=settings, public_url='')
    monkeypatch.setattr(cli, 'configured_service', lambda: (service, Mock(), Mock()))
    scheduler = Mock()
    monkeypatch.setattr('jobhunter_service.scheduler.Scheduler', Mock(return_value=scheduler))
    handler = Mock()
    monkeypatch.setattr('jobhunter_service.dispatch.ApplicationTelegramHandler', Mock(return_value=handler))
    ingress = Mock()
    ingress_factory = Mock(return_value=ingress)
    monkeypatch.setattr('jobhunter_service.telegram_ingress.TelegramIngress', ingress_factory)
    app_factory = Mock()
    monkeypatch.setattr('jobhunter_service.web.create_app', app_factory)
    stop = threading.Event()
    monkeypatch.setattr(cli.threading, 'Event', lambda: stop)
    started = {}

    class Thread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            started[self.target.__name__] = self.target

    monkeypatch.setattr(cli.threading, 'Thread', Thread)
    handler.poll_once.side_effect = stop.set
    ingress.drain_one.side_effect = stop.set

    def run_app(*args, **kwargs):
        assert set(started) == {receiver, 'scheduling', 'work', 'monitoring'}
        started[receiver]()

    monkeypatch.setattr(cli.web, 'run_app', run_app)
    cli.serve()
    if mode == 'shared':
        handler.poll_once.assert_not_called()
        ingress.drain_one.assert_called_once()
        assert app_factory.call_args.kwargs['telegram_ingress'] is ingress
    else:
        ingress_factory.assert_not_called()
        handler.poll_once.assert_called_once()
        assert app_factory.call_args.kwargs['telegram_ingress'] is None
