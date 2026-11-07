import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    owner: int
    url: str
    directory: str
    password: str = field(repr=False)
    username: str = "opencode"
    agent: str = ""
    state: Path = Path("data/agentbot.sqlite3")
    poll: float = 5
    command: tuple[str, ...] = field(default=(), repr=False)
    proxy: str = field(default="", repr=False)


def load() -> Settings:
    token = os.environ.get("BOT_TOKEN", "").strip()
    owner = int(os.environ.get("OWNER_ID", "0"))
    command = tuple(shlex.split(os.environ.get("OPENCODE_COMMAND", "")))
    url = os.environ.get("OPENCODE_URL", "http://127.0.0.1:4096").rstrip("/")
    parsed = urlsplit(url)
    if not command and (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Invalid OPENCODE_URL")
    if not command and parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("HTTP requires a loopback SSH forward")
    directory = os.environ.get("OPENCODE_DIRECTORY", "")
    password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
    poll = float(os.environ.get("POLL_SECONDS", "5"))
    proxy = os.environ.get("PROXY_URL", "").strip()
    if proxy:
        proxy_url = urlsplit(proxy)
        if proxy_url.scheme not in {"http", "https", "socks4", "socks5"} or not proxy_url.hostname or proxy_url.query or proxy_url.fragment:
            raise ValueError("Invalid PROXY_URL")
        # Reject malformed port syntax without ever exposing the proxy URL.
        try:
            proxy_url.port
        except ValueError:
            raise ValueError("Invalid PROXY_URL") from None
    if not token or owner <= 0 or not directory.startswith("/") or not (password or command) or not 2 <= poll <= 60:
        raise ValueError("Set BOT_TOKEN, OWNER_ID, OPENCODE_DIRECTORY and API password or OPENCODE_COMMAND; POLL_SECONDS=2..60")
    return Settings(token, owner, url, directory, password,
                    os.environ.get("OPENCODE_SERVER_USERNAME", "opencode"),
                    os.environ.get("OPENCODE_AGENT", ""),
                    Path(os.environ.get("STATE_FILE", "data/agentbot.sqlite3")), poll, command, proxy)
