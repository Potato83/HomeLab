from dataclasses import dataclass
from os import getenv

from aiohttp import ClientError, ClientSession, ClientTimeout

from serverbot.config import HttpCheckConfig


@dataclass(frozen=True)
class HttpReading:
    name: str
    ok: bool
    status: int | None
    error: str | None


async def get_http_status(check: HttpCheckConfig, session: ClientSession) -> HttpReading:
    headers = {}
    # Authentication is optional; auth_env names the variable containing the header value
    if check.auth_env:
        secret = getenv(check.auth_env, "").strip()
        if not secret:
            return HttpReading(check.name, False, None, "Missing authentication setting")
        headers[check.auth_header] = secret
    try:
        # Bound the whole request and its connection/read stages
        timeout = ClientTimeout(total=check.timeout, connect=check.timeout, sock_read=check.timeout)
        async with session.get(check.url, timeout=timeout, headers=headers, allow_redirects=False) as response:
            # Check status only; do not parse the body or follow a redirect to a login page
            ok = response.status == check.expected_status
            return HttpReading(check.name, ok, response.status, None if ok else "Unexpected HTTP status")
    except TimeoutError:
        return HttpReading(check.name, False, None, "Timeout")
    except (ClientError, OSError, ValueError):
        # Exception messages can contain URLs or authentication details
        return HttpReading(check.name, False, None, "HTTP request failed")
