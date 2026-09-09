"""Run Hermes as an ephemeral JSON planner with no executable tools or memory."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


class HermesPlanner:
    def __init__(self, python, source, *, model, provider, api_key, base_url=None, timeout=180):
        # Preserve the venv launcher symlink so Python locates pyvenv.cfg.
        self.python = str(Path(python).expanduser().absolute())
        self.source = str(Path(source).resolve())
        self.model, self.provider, self.api_key, self.base_url = model, provider, api_key, base_url
        self.timeout = timeout
        if not model or not provider or not api_key:
            raise ValueError('A dedicated JobHunter model, provider and API key are required.')

    def __call__(self, messages, response_schema):
        if len(json.dumps(messages)) > 750000:
            raise ValueError('The review batch exceeds the configured context limit.')
        with tempfile.TemporaryDirectory(prefix='jobhunter-hermes-') as state:
            env = {key: value for key, value in os.environ.items()
                   if key in {'PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE'}}
            env.update({'HERMES_HOME': state, 'PYTHON_DOTENV_DISABLED': '1',
                        'JOBHUNTER_HERMES_SOURCE': self.source, 'PYTHONUNBUFFERED': '1'})
            request = {'messages': messages, 'schema': response_schema, 'model': self.model,
                       'provider': self.provider, 'api_key': self.api_key, 'base_url': self.base_url}
            try:
                result = subprocess.run([self.python, str(Path(__file__).resolve()), '--child'],
                                        input=json.dumps(request), text=True, capture_output=True,
                                        cwd=state, env=env, timeout=self.timeout)
            except (OSError, subprocess.TimeoutExpired):
                raise RuntimeError('The restricted Hermes planner is unavailable. No changes were applied.') from None
            if result.returncode or len(result.stdout) > 250000:
                raise RuntimeError('Hermes could not produce a scoped response. No changes were applied.')
            try:
                return json.loads(result.stdout)
            except ValueError:
                raise RuntimeError('Hermes returned an invalid response. No changes were applied.') from None


def child():
    request = json.load(sys.stdin)
    source = Path(os.environ['JOBHUNTER_HERMES_SOURCE'])
    sys.path.insert(0, str(source))
    # Third-party startup output must not reach protocol output or expose secrets.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from run_agent import AIAgent
        from toolsets import TOOLSETS
        agent = AIAgent(model=request['model'], provider=request['provider'],
                        api_key=request['api_key'], base_url=request.get('base_url'),
                        enabled_toolsets=[], disabled_toolsets=list(TOOLSETS),
                        max_iterations=1, max_tokens=12000, run_budget_seconds=150,
                        skip_memory=True, skip_background_review=True, skip_context_files=True,
                        load_soul_identity=False, session_db=None, save_trajectories=False,
                        checkpoints_enabled=False, quiet_mode=True)
        if agent.tools:
            raise RuntimeError('Hermes tool isolation could not be established.')
        messages = request['messages']
        system = ('Return one JSON value matching this schema. Never execute tools or claim changes were applied.\n'
                  + json.dumps(request['schema']))
        if messages and messages[0].get('role') == 'system':
            system += '\n' + messages[0]['content']
            messages = messages[1:]
        response = agent.run_conversation(json.dumps(messages, ensure_ascii=False), system_message=system)
        final = response.get('final_response', '')
        if final.startswith('```'):
            final = final.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        value = json.loads(final)
        agent.close()
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    try:
        child()
    except Exception:
        print('Restricted Hermes request failed.', file=sys.stderr)
        raise SystemExit(1)
