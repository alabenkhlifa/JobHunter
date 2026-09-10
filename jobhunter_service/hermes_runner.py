"""Run Hermes as an ephemeral JSON planner with no executable tools or memory."""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path


# Legacy API-key setup remains available. Shared owner grants also carry the
# exact transport and endpoint, so OAuth tokens need no environment variables.
PROVIDER_KEY_ENV = {
    'openai': 'OPENAI_API_KEY',
    'openai-api': 'OPENAI_API_KEY',
    'openrouter': 'OPENROUTER_API_KEY',
    'anthropic': 'ANTHROPIC_API_KEY',
    'gemini': 'GOOGLE_API_KEY',
    'custom': 'OPENAI_API_KEY',
}


class PlannerAuthenticationError(RuntimeError):
    pass


class HermesPlanner:
    def __init__(self, python, source, *, model, provider, api_key, base_url=None,
                 api_mode=None, timeout=180):
        # Preserve the venv launcher symlink so Python locates pyvenv.cfg.
        self.python = str(Path(python).expanduser().absolute())
        self.source = str(Path(source).resolve())
        self.model, self.provider, self.api_key, self.base_url = model, provider, api_key, base_url
        self.api_mode = api_mode
        self.timeout = timeout
        if not model or not provider or not api_key:
            raise ValueError('A JobHunter model, provider and access credential are required.')
        if api_mode is not None:
            from jobhunter_service.owner_auth import validate_runtime
            validate_runtime({'model': model, 'provider': provider, 'api_key': api_key,
                              'base_url': base_url, 'api_mode': api_mode})
        elif provider not in PROVIDER_KEY_ENV:
            raise ValueError('The restricted Hermes planner requires a supported API-key provider.')
        if provider == 'custom' and (not isinstance(base_url, str) or not base_url.strip()):
            raise ValueError('A custom Hermes provider requires an explicit base URL.')

    def __call__(self, messages, response_schema):
        if len(json.dumps(messages)) > 750000:
            raise ValueError('The review batch exceeds the configured context limit.')
        with tempfile.TemporaryDirectory(prefix='jobhunter-hermes-') as state:
            env = {key: value for key, value in os.environ.items()
                   if key in {'PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE'}}
            env.update({'HOME': state, 'HERMES_HOME': state, 'PYTHON_DOTENV_DISABLED': '1',
                        'JOBHUNTER_HERMES_SOURCE': self.source, 'PYTHONUNBUFFERED': '1'})
            request = {'messages': messages, 'schema': response_schema, 'model': self.model,
                       'provider': self.provider, 'api_key': self.api_key, 'base_url': self.base_url,
                       'api_mode': self.api_mode}
            try:
                result = subprocess.run([self.python, str(Path(__file__).resolve()), '--child'],
                                        input=json.dumps(request), text=True, capture_output=True,
                                        cwd=state, env=env, timeout=self.timeout)
            except (OSError, subprocess.TimeoutExpired):
                raise RuntimeError('The restricted Hermes planner is unavailable. No changes were applied.') from None
            if result.returncode == 77:
                raise PlannerAuthenticationError('Hermes authentication needs refreshing.')
            if result.returncode or len(result.stdout) > 250000:
                raise RuntimeError('Hermes could not produce a scoped response. No changes were applied.')
            try:
                return json.loads(result.stdout)
            except ValueError:
                raise RuntimeError('Hermes returned an invalid response. No changes were applied.') from None


class SharedOwnerPlanner:
    def __init__(self, python, source, socket_path, *, timeout=180):
        from jobhunter_service.owner_auth import OwnerAuthClient
        self.python, self.source, self.timeout = python, source, timeout
        self.auth = OwnerAuthClient(socket_path)

    def __call__(self, messages, response_schema):
        if len(json.dumps(messages)) > 750000:
            raise ValueError('The review batch exceeds the configured context limit.')
        runtime = self.auth.resolve()
        for attempt in range(2):
            planner = HermesPlanner(self.python, self.source, timeout=self.timeout,
                **{key: runtime[key] for key in ('model', 'provider', 'api_key', 'base_url', 'api_mode')})
            try:
                return planner(messages, response_schema)
            except PlannerAuthenticationError:
                if attempt:
                    raise RuntimeError('Hermes authentication is unavailable. No changes were applied.') from None
                runtime = self.auth.resolve(refresh_ticket=runtime['refresh_ticket'])
            finally:
                # Access credentials are request-local; never retain them on
                # the shared planner or write them to its temporary home.
                planner.api_key = None


def child():
    request = json.load(sys.stdin)
    if request.get('api_mode') is not None:
        # This script is launched by absolute filename with a clean environment.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from jobhunter_service.owner_auth import validate_runtime
        validate_runtime(request)
    elif request.get('provider') not in PROVIDER_KEY_ENV or not request.get('api_key'):
        raise ValueError('A supported provider and access credential are required.')
    if request['provider'] == 'custom' and not str(request.get('base_url') or '').strip():
        raise ValueError('A custom Hermes provider requires an explicit base URL.')
    # Set only the requested provider's key inside this disposable subprocess.
    # The parent environment and owner credentials never enter this process.
    if request['provider'] in PROVIDER_KEY_ENV:
        os.environ[PROVIDER_KEY_ENV[request['provider']]] = request['api_key']
    # This pinned AIAgent uses openai-api; openai is only an auxiliary alias.
    provider = 'openai-api' if request['provider'] == 'openai' else request['provider']
    source = Path(os.environ['JOBHUNTER_HERMES_SOURCE'])
    sys.path.insert(0, str(source))
    logging.disable(sys.maxsize)
    # Third-party startup output must not reach protocol output or expose secrets.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from run_agent import AIAgent
        from toolsets import TOOLSETS
        agent = AIAgent(model=request['model'], provider=provider,
                        api_key=request['api_key'], base_url=request.get('base_url'),
                        api_mode=request.get('api_mode'),
                        enabled_toolsets=[], disabled_toolsets=list(TOOLSETS),
                        max_iterations=1, max_tokens=12000, run_budget_seconds=150,
                        skip_memory=True, skip_background_review=True, skip_context_files=True,
                        load_soul_identity=False, session_db=None, save_trajectories=False,
                        checkpoints_enabled=False, quiet_mode=True)
        try:
            if agent.tools:
                raise RuntimeError('Hermes tool isolation could not be established.')
            auth_failed = []
            def owner_refresh_required(*args, **kwargs):
                # Pinned Hermes calls these hooks only after a concrete 401.
                # Keep refresh and auth-file access in the owner broker.
                auth_failed.append(True)
                return False
            agent._try_refresh_codex_client_credentials = owner_refresh_required
            agent._try_refresh_nous_client_credentials = owner_refresh_required
            messages = request['messages']
            system = ('Return one JSON value matching this schema. Never execute tools or claim changes were applied.\n'
                      + json.dumps(request['schema']))
            if messages and messages[0].get('role') == 'system':
                system += '\n' + messages[0]['content']
                messages = messages[1:]
            response = agent.run_conversation(json.dumps(messages, ensure_ascii=False), system_message=system)
            if auth_failed:
                raise PlannerAuthenticationError('Hermes authentication needs refreshing.')
            if response.get('error'):
                raise RuntimeError('Hermes could not produce a scoped response.')
            final = response.get('final_response', '')
            if final.startswith('```'):
                final = final.split('\n', 1)[1].rsplit('```', 1)[0].strip()
            value = json.loads(final)
        finally:
            agent.close()
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    try:
        child()
    except Exception as error:
        print('Restricted Hermes request failed.', file=sys.stderr)
        raise SystemExit(77 if isinstance(error, PlannerAuthenticationError)
                         or getattr(error, 'status_code', None) == 401 else 1)
