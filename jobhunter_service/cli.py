"""Operator entrypoints; user identities come from Telegram, not command prompts."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import threading

import requests
from aiohttp import web


def configured_service():
    from .browser import DockerBrowserManager
    from .hermes_runner import HermesPlanner
    from .service import JobHunterService
    from .telegram import TelegramClient

    required = ['JOBHUNTER_SERVICE_BOT_TOKEN', 'JOBHUNTER_OWNER_TELEGRAM_USER_ID',
                'JOBHUNTER_SERVICE_DATA_ROOT', 'JOBHUNTER_ADMIN_TOKEN',
                'JOBHUNTER_HERMES_PYTHON', 'JOBHUNTER_HERMES_SOURCE', 'JOBHUNTER_MODEL',
                'JOBHUNTER_MODEL_PROVIDER', 'JOBHUNTER_MODEL_API_KEY']
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise ValueError('Missing operator settings: ' + ', '.join(missing))
    token = os.environ['JOBHUNTER_ADMIN_TOKEN']
    if len(token) < 32:
        raise ValueError('The admin token must contain at least 32 random characters.')
    client = TelegramClient(os.environ['JOBHUNTER_SERVICE_BOT_TOKEN'])
    browsers = DockerBrowserManager() if os.environ.get('JOBHUNTER_BROWSER_ENABLED') == 'true' else None
    service = JobHunterService(os.environ['JOBHUNTER_SERVICE_DATA_ROOT'],
        int(os.environ['JOBHUNTER_OWNER_TELEGRAM_USER_ID']),
        public_url=os.environ.get('JOBHUNTER_PUBLIC_URL', ''), telegram_client=client, browser_manager=browsers)
    planner = HermesPlanner(os.environ['JOBHUNTER_HERMES_PYTHON'], os.environ['JOBHUNTER_HERMES_SOURCE'],
        model=os.environ['JOBHUNTER_MODEL'], provider=os.environ['JOBHUNTER_MODEL_PROVIDER'],
        api_key=os.environ['JOBHUNTER_MODEL_API_KEY'], base_url=os.environ.get('JOBHUNTER_MODEL_BASE_URL'))
    return service, client, planner


def serve():
    from .hermes import RestrictedHermesAssistant
    from .scheduler import Scheduler
    from .telegram import polling_lock
    from .web import create_app
    service, client, planner = configured_service()
    stop = threading.Event()
    scheduler = Scheduler(service, planner, client)
    # Optional application facade is installed only after dependencies import.
    from .dispatch import ApplicationTelegramHandler
    handler = ApplicationTelegramHandler(service, client, RestrictedHermesAssistant(planner), scheduler)
    google_client = None
    if os.environ.get('JOBHUNTER_GOOGLE_WEB_CLIENT'):
        from jobhunter_integrations.web_oauth import GoogleOAuthClient
        google_client = GoogleOAuthClient(os.environ['JOBHUNTER_GOOGLE_WEB_CLIENT'],
                                         service.public_url + '/oauth/google/callback')
    app = create_app(service, google_client=google_client, admin_token=os.environ['JOBHUNTER_ADMIN_TOKEN'])

    def polling():
        while not stop.is_set():
            try:
                handler.poll_once()
            except Exception as error:
                logging.getLogger('jobhunter').warning('Telegram poll failed (%s); update retained.', type(error).__name__)
                stop.wait(5)

    def scheduling():
        while not stop.is_set():
            try:
                scheduler.queue_due()
            except Exception as error:
                logging.getLogger('jobhunter').warning('Background operation failed (%s); pending work retained.', type(error).__name__)
            stop.wait(20)

    def work():
        scheduler.recover()
        while not stop.is_set():
            try:
                scheduler.delivery.drain()
                if scheduler.run_next():
                    continue
            except Exception as error:
                logging.getLogger('jobhunter').warning('Background operation failed (%s); pending work retained.', type(error).__name__)
            stop.wait(10)

    def monitoring():
        while not stop.is_set():
            try:
                scheduler.sync_due()
                scheduler.monitor_due()
            except Exception as error:
                logging.getLogger('jobhunter').warning('Background operation failed (%s); pending work retained.', type(error).__name__)
            stop.wait(60)

    with polling_lock(service.root / 'service' / 'daemon.lock'):
        threads = [threading.Thread(target=target, daemon=True) for target in (polling, scheduling, work, monitoring)]
        for thread in threads:
            thread.start()
        try:
            web.run_app(app, host='127.0.0.1', port=int(os.environ.get('JOBHUNTER_HTTP_PORT', '8765')),
                        access_log=None, print=None)
        finally:
            stop.set()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('serve')
    sub.add_parser('check')
    admin = sub.add_parser('admin')
    admin.add_argument('operation', choices=['add', 'list', 'suspend', 'revoke'])
    admin.add_argument('user_id', nargs='?', type=int)
    admin.add_argument('--config', type=Path, default=Path.home() / '.jobhunter' / 'admin-client.json')
    args = parser.parse_args(argv)
    try:
        if args.command == 'serve':
            serve()
        elif args.command == 'check':
            configured_service()
            print('JobHunter operator configuration is valid; no external connection was made.')
        else:
            if args.operation != 'list' and (not args.user_id or args.user_id <= 0):
                raise ValueError('A positive Telegram user ID is required.')
            config = json.loads(args.config.read_text())
            port = config.get('port', 8765)
            if type(port) is not int or not 1024 <= port <= 65535 or len(config['token']) < 32:
                raise ValueError('Invalid local admin client configuration.')
            session = requests.Session()
            session.trust_env = False
            response = session.post(f'http://127.0.0.1:{port}/admin',
                headers={'Authorization': 'Bearer ' + config['token']},
                json={'operation': args.operation, 'user_id': args.user_id}, timeout=20, allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError('The local registration service did not accept the request.')
            print(json.dumps(response.json(), indent=2))
        return 0
    except Exception as error:
        # Configuration errors contain key names, never values. Provider/HTTP
        # exceptions are deliberately not printed because URLs may hold tokens.
        print(str(error) if isinstance(error, ValueError) else 'JobHunter operation failed. Check the local service configuration.')
        return 1
