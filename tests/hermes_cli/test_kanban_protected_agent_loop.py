"""One real protected Hermes task through a local deterministic model endpoint."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.hermes_cli.test_kanban_protected_authority import protected_board
from tests.hermes_cli.test_kanban_execution_scope import wait_for
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch, kanban_execution_scope as scopes


@pytest.mark.linux_only
def test_protected_actual_agent_completes_task_through_local_model(protected_board, monkeypatch):
    home, public, authority, worker = protected_board
    monkeypatch.delenv('HERMES_KANBAN_WORKSPACES_ROOT')
    home.chmod(0o700)
    requests = []
    completed_calls = []
    task_id = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            payload = json.dumps({'object': 'list', 'data': [{'id': 'fixture-local', 'object': 'model'}]}).encode()
            self.send_response(200);self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)));self.end_headers();self.wfile.write(payload)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('content-length', 0))) or b'{}')
            if not self.path.endswith('/chat/completions'):
                self.send_response(404);self.end_headers();return
            names = [item.get('function', {}).get('name') for item in body.get('tools', [])]
            requests.append({'path': self.path, 'model': body.get('model'), 'tools': names})
            message = {'role': 'assistant', 'content': 'Local fixture task completed.'}
            reason = 'stop'
            if 'kanban_complete' in names and not completed_calls:
                completed_calls.append(task_id)
                message = {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'fixture-complete', 'type': 'function', 'function': {
                        'name': 'kanban_complete', 'arguments': json.dumps({
                            'task_id': task_id, 'summary': 'Completed through the real protected agent loop.'})}}]}
                reason = 'tool_calls'
            if body.get('stream'):
                delta = dict(message)
                if 'tool_calls' in delta:
                    delta['tool_calls'] = [{'index': index, **call} for index, call in enumerate(delta['tool_calls'])]
                chunks = [
                    {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture-local',
                     'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                    {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture-local',
                     'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}],
                     'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}},
                ]
                payload = ''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks).encode()+b'data: [DONE]\n\n'
                content_type = 'text/event-stream'
            else:
                payload = json.dumps({'id': 'fixture', 'object': 'chat.completion', 'created': 1, 'model': 'fixture-local',
                                      'choices': [{'index': 0, 'message': message, 'finish_reason': reason}],
                                      'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}).encode()
                content_type = 'application/json'
            self.send_response(200);self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)));self.end_headers();self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    profile = authority.worker_profile('fixture')
    config = {'model': {'default': 'fixture-local', 'provider': 'custom',
                        'base_url': f'http://127.0.0.1:{server.server_address[1]}/v1',
                        'api_key': 'local-fixture-only', 'api_mode': 'chat_completions'},
              'platform_toolsets': {'cli': ['kanban']}, 'agent': {'max_turns': 3},
              'compression': {'enabled': False}}
    (profile/'config.yaml').write_text(json.dumps(config))
    try:
        with kbc.connect_closing() as conn:
            task_id = kb.create_task(conn, title='Local native agent pilot',
                                     body='Complete this fixture task using kanban_complete.', assignee='fixture',
                                     workspace_kind='scratch', skills=[], max_runtime_seconds=60,
                                     model_override='fixture-local', provider_override='custom')
            result = dispatch.dispatch_once(conn, max_in_progress=1)
            task = kb.get_task(conn, task_id)
            run_id = task.current_run_id
            try:
                assert result.spawned, task.last_failure_error
                wait_for(lambda: scopes.settled(conn, run_id), timeout=70)
                output = kb.read_worker_log(task_id, tail_bytes=24000) or ''
                print('ACTUAL_AGENT_OUTPUT='+output, flush=True)
                print('LOCAL_MODEL_REQUESTS='+json.dumps(requests), flush=True)
                scope = authority.read(conn, run_id)
                assert scope['returncode'] == 0, output
                assert completed_calls == [task_id], output
                task = kb.get_task(conn, task_id)
                assert task.status == 'done', output
                assert not authority.pending(conn, task_id)
                assert scope['children_reaped'] and scope['worker_pid']
                assert not Path(task.workspace_path).exists()
                assert home.stat().st_mode & 0o777 == 0o700
            finally:
                scopes.request_task_stop(conn, task_id)
                wait_for(lambda: scopes.settled(conn, run_id), timeout=10)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
