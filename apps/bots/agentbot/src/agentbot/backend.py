import asyncio
import contextlib
import json
import base64
import uuid
from pathlib import Path
from urllib.parse import quote

from aiohttp import BasicAuth, ClientSession, ClientTimeout, DummyCookieJar

from .config import Settings

REQUEST_TIMEOUT = 20
MAX_RESPONSE = 16 * 1024 * 1024


class BackendError(Exception):
    """Contains no response body, URL, task or credentials."""


class OpenCode:
    """Small adapter for the native OpenCode 2 HTTP API."""

    def __init__(self, settings: Settings):
        self.settings = settings
        # OpenCode 2 uses the fixed HTTP Basic username "opencode".
        self.http = None if settings.command else ClientSession(
            auth=BasicAuth("opencode", settings.password),
            timeout=ClientTimeout(total=REQUEST_TIMEOUT), cookie_jar=DummyCookieJar())

    async def close(self):
        if self.http is not None:
            await self.http.close()

    async def request(self, method, path, body=None, *, params=None):
        try:
            if self.settings.command:
                return await self._request_command(method, path, body, params)
            async with self.http.request(method, self.settings.url + path,
                                         params=params,
                                         json=body, allow_redirects=False) as response:
                if response.status >= 300:
                    raise BackendError(f"HTTP {response.status}")
                if response.status == 204:
                    return None
                return await response.json()
        except BackendError:
            raise
        except Exception:
            raise BackendError("OpenCode connection or protocol error") from None

    async def _request_command(self, method, path, body, params):
        # The fixed, trusted command receives request content on stdin, never argv.
        payload = json.dumps({"method": method, "path": path, "body": body, "params": params}).encode()
        process = await asyncio.create_subprocess_exec(
            *self.settings.command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

        async def exchange():
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            output = bytearray()
            while chunk := await process.stdout.read(65536):
                if len(output) + len(chunk) > MAX_RESPONSE:
                    raise BackendError("Command response too large")
                output.extend(chunk)
            await process.wait()
            if process.returncode != 0:
                raise BackendError("OpenCode command failed")
            result = json.loads(output)
            if not isinstance(result, dict) or result.get("ok") is not True or "data" not in result:
                # Even an error marked safe by the helper is never echoed into logs.
                raise BackendError("OpenCode command request failed")
            return result["data"]

        try:
            return await asyncio.wait_for(exchange(), timeout=REQUEST_TIMEOUT)
        finally:
            # This also runs on cancellation, malformed output and size violations.
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def create(self):
        body = {"title": "AgentBot", "location": {"directory": self.settings.directory}}
        if self.settings.agent:
            body["agent"] = self.settings.agent
        result = await self.request("POST", "/api/session", body)
        return result["data"]["id"]

    async def statuses(self):
        result = await self.request("GET", "/api/session/active")
        return {sid: {"type": "busy"} for sid in result["data"]}

    async def messages(self, sid):
        path = f"/api/session/{quote(sid, safe='')}/message"
        params = {"order": "asc", "limit": 200}
        messages = []
        seen = set()
        while True:
            result = await self.request("GET", path, params=params)
            messages.extend(result["data"])
            cursor = result.get("cursor", {}).get("next")
            if not cursor:
                break
            if cursor in seen:
                raise BackendError("Repeated message cursor")
            seen.add(cursor)
            # A cursor already carries ordering; v2 rejects cursor + order.
            params = {"cursor": cursor, "limit": 200}
        normalized = normalize_messages(messages)
        inbox = await self.request("GET", f"/api/session/{quote(sid, safe='')}/inbox")
        known = {message["info"]["id"] for message in normalized}
        for item in inbox["data"]:
            # Durable admission can precede projection into the transcript.
            if item.get("type") == "user" and item["id"] not in known:
                normalized.append({"info": {"id": item["id"], "role": "user"}, "parts": []})
        return normalized

    async def submit(self, sid, mid, prompt):
        body = {"id": mid, "text": prompt, "delivery": "queue", "resume": True}
        result = await self.request("POST", f"/api/session/{quote(sid, safe='')}/prompt", body)
        if result["data"]["id"] != mid:
            raise BackendError("Prompt admission not confirmed")

    async def abort(self, sid, mid):
        base = f"/api/session/{quote(sid, safe='')}"
        # Interrupting a drain alone does not remove its durable queued input.
        # Cancel only this bot's known message; unavailable input is an API no-op.
        await self.request("DELETE", f"{base}/inbox/{quote(mid, safe='')}")
        result = await self.request("POST", f"{base}/interrupt", params={"resume": "false"})
        if not isinstance(result.get("interrupted"), bool):
            raise BackendError("Abort not confirmed")

    async def intake(self, sid, data):
        if not self.settings.command:
            if data.get('kind') == 'task':
                return {'prompt': data.get('description') or data.get('title'), 'files': []}
            raise BackendError('Board intake requires private transport')
        return await self.request('POST', f'/agentbot/session/{quote(sid,safe="")}/intake', data)

    async def save_access(self, sid, access):
        return await self.request('POST', f'/agentbot/session/{quote(sid,safe="")}/access', access)

    async def upload(self, sid, path: Path, name: str, upload_id=None):
        if not self.settings.command:
            raise BackendError('File intake requires private transport')
        total = path.stat().st_size
        if total > 8*1024*1024:
            raise ValueError('File too large')
        uid = upload_id or uuid.uuid4().hex
        with path.open('rb') as file:
            offset = 0
            while True:
                chunk = file.read(32768)
                result = await self.request('POST', f'/agentbot/session/{quote(sid,safe="")}/file',
                                            {'upload_id':uid,'name':name,'offset':offset,'total':total,
                                             'data':base64.b64encode(chunk).decode()})
                offset += len(chunk)
                if offset >= total:
                    return {**result, 'upload_id': uid}


def normalize_messages(messages):
    """Adapt native turns to the small reply shape consumed by Service.

    Native assistant messages have no parent ID. An idle marker closes a turn,
    including any additional prompts steered into it. Shutdown is not completion.
    Only user IDs and assistant text are retained, never reasoning or tool output.
    """
    output = []
    users, assistants = [], []

    def emit(idle=None):
        for uid in users:
            for assistant in assistants:
                output.append({
                    "info": {"id": assistant["id"], "role": "assistant", "parentID": uid,
                             "time": {}, "finish": assistant.get("finish")},
                    "parts": [{"type": "text", "text": part["text"]}
                              for part in assistant.get("content", [])
                              if part.get("type") == "text" and isinstance(part.get("text"), str)],
                })
            if idle is not None:
                info = {"id": idle["id"], "role": "assistant", "parentID": uid,
                        "finish": "stop", "time": {"completed": idle.get("time", {}).get("created") or 1}}
                if idle.get("outcome") != "succeeded":
                    info["error"] = {"type": "session_failed_or_interrupted"}
                output.append({"info": info, "parts": []})

    for message in messages:
        kind = message.get("type")
        if kind == "user":
            uid = message["id"]
            users.append(uid)
            output.append({"info": {"id": uid, "role": "user"}, "parts": []})
        elif kind == "assistant":
            assistants.append(message)
        elif kind == "idle":
            emit(message)
            users, assistants = [], []
    emit()
    return output
