import asyncio
import json
import sys
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from agentbot.backend import BackendError, OpenCode, normalize_messages
import agentbot.backend as backend_module
from agentbot.config import Settings
from agentbot.service import completion


def backend():
    client = object.__new__(OpenCode)
    client.settings = Settings('dummy-token', 123, 'http://127.0.0.1:4096',
                               '/example/project', 'dummy-password', agent='build')
    client.request = AsyncMock()
    return client


def user(mid):
    return {'id': mid, 'type': 'user', 'text': 'private fixture'}


def assistant(mid='msg_answer', text='4'):
    return {'id': mid, 'type': 'assistant', 'time': {'completed': 100}, 'finish': 'stop',
            'content': [{'type': 'text', 'text': text},
                        {'type': 'reasoning', 'text': 'private reasoning'},
                        {'type': 'tool', 'state': {'content': 'private tool output'}}]}


def idle(outcome='succeeded'):
    return {'id': 'msg_idle', 'type': 'idle', 'outcome': outcome, 'time': {'created': 200}}


def test_v2_create_uses_location_and_configured_agent():
    async def run():
        client = backend()
        client.request.return_value = {'data': {'id': 'ses_example'}}
        assert await client.create() == 'ses_example'
        client.request.assert_awaited_once_with('POST', '/api/session', {
            'title': 'AgentBot', 'location': {'directory': '/example/project'}, 'agent': 'build'})
    asyncio.run(run())


def test_v2_submit_uses_durable_message_id_and_text():
    async def run():
        client = backend()
        client.request.return_value = {'data': {'id': 'msg_input'}}
        await client.submit('ses_example', 'msg_input', 'task')
        client.request.assert_awaited_once_with('POST', '/api/session/ses_example/prompt', {
            'id': 'msg_input', 'text': 'task', 'delivery': 'queue', 'resume': True})
        client.request.return_value = {'data': {'id': 'msg_different'}}
        with pytest.raises(BackendError):
            await client.submit('ses_example', 'msg_input', 'task')
    asyncio.run(run())


def test_active_sessions_normalized_to_busy():
    async def run():
        client = backend()
        client.request.return_value = {'data': {'ses_example': {'type': 'running'}}}
        assert await client.statuses() == {'ses_example': {'type': 'busy'}}
        client.request.assert_awaited_once_with('GET', '/api/session/active')
    asyncio.run(run())


def test_pagination_does_not_combine_cursor_and_order_and_inbox_confirms_admission():
    async def run():
        client = backend()
        client.request.side_effect = [
            {'data': [user('msg_input')], 'cursor': {'next': 'opaque cursor'}},
            {'data': [assistant(), idle()], 'cursor': {}},
            {'data': [{'id': 'msg_queued', 'type': 'user'},
                      {'id': 'msg_input', 'type': 'user'},
                      {'id': 'msg_other', 'type': 'compaction'}]},
        ]
        result = await client.messages('ses_example')
        assert completion(result, 'msg_input', {}) == ('done', '4')
        assert sum(item['info']['id'] == 'msg_input' for item in result) == 1
        assert any(item['info']['id'] == 'msg_queued' for item in result)
        assert not any(item['info']['id'] == 'msg_other' for item in result)
        calls = client.request.await_args_list
        assert calls[0].kwargs == {'params': {'order': 'asc', 'limit': 200}}
        assert calls[1].kwargs == {'params': {'cursor': 'opaque cursor', 'limit': 200}}
        assert calls[2].args == ('GET', '/api/session/ses_example/inbox')
    asyncio.run(run())


def test_repeated_cursor_fails_without_looping():
    async def run():
        client = backend()
        client.request.return_value = {'data': [], 'cursor': {'next': 'same'}}
        with pytest.raises(BackendError, match='Repeated message cursor'):
            await client.messages('ses_example')
        assert client.request.await_count == 2
    asyncio.run(run())


def test_abort_cancels_only_own_input_then_interrupts_without_resume():
    async def run():
        client = backend()
        client.request.side_effect = [None, {'interrupted': False}]
        await client.abort('ses_example', 'msg_own')
        calls = client.request.await_args_list
        assert len(calls) == 2
        assert calls[0].args == ('DELETE', '/api/session/ses_example/inbox/msg_own')
        assert calls[1].args == ('POST', '/api/session/ses_example/interrupt')
        assert calls[1].kwargs == {'params': {'resume': 'false'}}
    asyncio.run(run())


def test_shutdown_without_idle_is_not_completion_and_private_parts_are_excluded():
    result = normalize_messages([user('msg_input'), assistant()])
    assert completion(result, 'msg_input', {}) is None
    assert 'private reasoning' not in repr(result)
    assert 'private tool output' not in repr(result)
    assert 'private fixture' not in repr(result)


@pytest.mark.parametrize('outcome', ['failed', 'interrupted'])
def test_idle_failure_without_assistant_is_terminal_and_sanitized(outcome):
    marker = idle(outcome)
    marker['error'] = {'message': 'private API error'}
    result = normalize_messages([user('msg_input'), marker])
    assert completion(result, 'msg_input', {})[0] == 'error'
    assert 'private API error' not in repr(result)


def test_idle_success_without_text_is_terminal():
    result = normalize_messages([user('msg_input'), idle()])
    assert completion(result, 'msg_input', {})[0] == 'done'


def test_old_turn_cannot_finish_new_turn():
    result = normalize_messages([user('msg_old'), assistant(text='old'), idle(),
                                 user('msg_new'), assistant('msg_new_answer', 'new')])
    assert completion(result, 'msg_old', {}) == ('done', 'old')
    assert completion(result, 'msg_new', {}) is None


def test_steered_prompt_shares_native_turn_completion():
    result = normalize_messages([user('msg_first'), assistant(text='first'),
                                 user('msg_second'), assistant('msg_second_answer', 'second'), idle()])
    assert completion(result, 'msg_first', {}) == ('done', 'first\nsecond')
    assert completion(result, 'msg_second', {}) == ('done', 'first\nsecond')


def test_http_transport_has_no_global_directory_query_and_sanitizes_errors():
    async def run():
        client = backend()
        client.request = OpenCode.request.__get__(client)
        response = Mock(status=401)
        context = AsyncMock()
        context.__aenter__.return_value = response
        client.http = Mock()
        client.http.request.return_value = context
        with pytest.raises(BackendError, match='HTTP 401'):
            await client.request('GET', '/api/session/active')
        client.http.request.assert_called_once_with(
            'GET', 'http://127.0.0.1:4096/api/session/active', params=None,
            json=None, allow_redirects=False)
        response.status = 200
        response.json = AsyncMock(side_effect=ValueError('private API body'))
        with pytest.raises(BackendError) as error:
            await client.request('GET', '/api/info')
        assert 'private API body' not in str(error.value)
    asyncio.run(run())


def command_backend(code):
    settings = backend().settings
    return OpenCode(replace(settings, password='', command=(sys.executable, '-c', code)))


def test_command_transport_stdin_protocol_and_no_http_session(monkeypatch):
    async def run():
        monkeypatch.setattr(backend_module, 'ClientSession', Mock(side_effect=AssertionError('HTTP not expected')))
        client = command_backend(
            'import json,sys; value=json.load(sys.stdin); '
            'assert len(sys.argv)==1; '
            'print(json.dumps({"ok":True,"data":value}))')
        prompt = 'private text $(must-not-execute) `nor-this`'
        try:
            result = await client.request('POST', '/api/session/example/prompt',
                                          {'text': prompt}, params={'resume': 'false'})
            assert result == {'method': 'POST', 'path': '/api/session/example/prompt',
                              'body': {'text': prompt}, 'params': {'resume': 'false'}}
            assert client.http is None
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize('code', [
    'import sys; print("private stderr",file=sys.stderr); sys.exit(1)',
    'print("invalid private data")',
    'print(\'{"ok":false,"error":"private error"}\')',
    'print(\'{"ok":true}\')',
])
def test_command_errors_do_not_disclose_output(code):
    async def run():
        client = command_backend(code)
        try:
            with pytest.raises(BackendError) as error:
                await client.request('GET', '/api/info')
            assert 'private' not in str(error.value)
        finally:
            await client.close()
    asyncio.run(run())


def test_command_transport_bounds_total_output(monkeypatch):
    async def run():
        monkeypatch.setattr(backend_module, 'MAX_RESPONSE', 100)
        client = command_backend('import sys; sys.stdin.read(); print("x"*200)')
        try:
            with pytest.raises(BackendError, match='too large'):
                await client.request('GET', '/api/info')
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize('cancel', [False, True])
def test_command_timeout_or_cancellation_reaps_process(monkeypatch, cancel):
    async def run():
        processes = []
        started = asyncio.Event()
        spawn = asyncio.create_subprocess_exec

        async def tracked_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            processes.append(process)
            started.set()
            return process

        monkeypatch.setattr(backend_module.asyncio, 'create_subprocess_exec', tracked_spawn)
        monkeypatch.setattr(backend_module, 'REQUEST_TIMEOUT', 0.2 if not cancel else 20)
        client = command_backend('import sys,time; sys.stdin.read(); time.sleep(30)')
        task = asyncio.create_task(client.request('GET', '/api/info'))
        try:
            await started.wait()
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(BackendError):
                    await task
            assert len(processes) == 1 and processes[0].returncode is not None
        finally:
            await client.close()
    asyncio.run(run())
