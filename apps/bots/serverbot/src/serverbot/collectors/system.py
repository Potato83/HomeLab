import asyncio
import ipaddress
import os
import re
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout

PROC_ROOT = Path("/proc")
MAX_LOG_BYTES = 512 * 1024
DOCKER_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)


@dataclass(frozen=True)
class ProcessReading:
    pid: int
    name: str
    cpu_percent: float
    memory_bytes: int
    memory_percent: float


@dataclass(frozen=True)
class SystemReading:
    cpu_percent: float
    memory_total_bytes: int
    memory_used_bytes: int
    memory_available_bytes: int
    uptime_seconds: float
    load_average: tuple[float, float, float]
    top_cpu: tuple[ProcessReading, ...]
    top_memory: tuple[ProcessReading, ...]


@dataclass(frozen=True)
class DockerReading:
    available: bool
    containers: tuple[tuple[str, str], ...] = ()
    error: str | None = None


def _safe_text(value: str, limit: int = 120) -> str:
    return "".join(char if char.isprintable() and char not in "\r\n\t" else "?" for char in value)[:limit]


def _cpu_totals() -> tuple[int, int]:
    fields = (PROC_ROOT / "stat").read_text(encoding="ascii").splitlines()[0].split()
    values = [int(value) for value in fields[1:]]
    # Guest counters are already included in user/nice and must not be counted twice.
    total = sum(values[:8])
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total, idle


def _process_snapshot() -> dict[int, tuple[str, int, int]]:
    page_size = os.sysconf("SC_PAGE_SIZE")
    processes = {}
    with os.scandir(PROC_ROOT) as entries:
        for entry in entries:
            if not entry.name.isascii() or not entry.name.isdigit():
                continue
            try:
                stat = Path(entry.path, "stat").read_text(encoding="utf-8", errors="replace")
                closing_paren = stat.rfind(")")
                if closing_paren < 0:
                    continue
                name = _safe_text(stat[stat.find("(") + 1:closing_paren]) or "unknown"
                fields = stat[closing_paren + 2:].split()
                if len(fields) < 22:
                    continue
                cpu_ticks = int(fields[11]) + int(fields[12])
                memory_bytes = max(0, int(fields[21])) * page_size
                processes[int(entry.name)] = (name, cpu_ticks, memory_bytes)
            except (OSError, ValueError):
                continue
    return processes


def _system_snapshot() -> tuple[int, int, dict[str, int], float, tuple[float, float, float], dict[int, tuple[str, int, int]]]:
    total_ticks, idle_ticks = _cpu_totals()
    memory = {}
    for line in (PROC_ROOT / "meminfo").read_text(encoding="ascii").splitlines():
        key, _, rest = line.partition(":")
        if key in {"MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached"}:
            memory[key] = int(rest.split()[0]) * 1024
    if "MemAvailable" not in memory:
        memory["MemAvailable"] = sum(memory.get(key, 0) for key in ("MemFree", "Buffers", "Cached"))
    uptime = float((PROC_ROOT / "uptime").read_text(encoding="ascii").split()[0])
    load = tuple(float(value) for value in (PROC_ROOT / "loadavg").read_text(encoding="ascii").split()[:3])
    if len(load) != 3 or "MemTotal" not in memory:
        raise OSError("Incomplete proc metrics")
    return total_ticks, idle_ticks, memory, uptime, load, _process_snapshot()


async def get_system_reading(top_processes: int = 5) -> SystemReading:
    before = _system_snapshot()
    await asyncio.sleep(0.15)
    after = _system_snapshot()
    total_delta = max(0, after[0] - before[0])
    idle_delta = max(0, after[1] - before[1])
    cpu_percent = (100 * (total_delta - min(total_delta, idle_delta)) / total_delta) if total_delta else 0.0
    memory_total = after[2]["MemTotal"]
    memory_available = min(memory_total, after[2].get("MemAvailable", 0))
    memory_used = memory_total - memory_available
    process_readings = []
    for pid, (name, cpu_ticks, memory_bytes) in after[5].items():
        previous = before[5].get(pid)
        process_cpu = 0.0
        if previous is not None and total_delta:
            process_cpu = max(0.0, 100 * (cpu_ticks - previous[1]) / total_delta)
        process_readings.append(ProcessReading(
            pid=pid,
            name=name,
            cpu_percent=process_cpu,
            memory_bytes=memory_bytes,
            memory_percent=100 * memory_bytes / memory_total if memory_total else 0.0,
        ))
    return SystemReading(
        cpu_percent=cpu_percent,
        memory_total_bytes=memory_total,
        memory_used_bytes=memory_used,
        memory_available_bytes=memory_available,
        uptime_seconds=after[3],
        load_average=after[4],
        top_cpu=tuple(sorted(process_readings, key=lambda process: process.cpu_percent, reverse=True)[:top_processes]),
        top_memory=tuple(sorted(process_readings, key=lambda process: process.memory_bytes, reverse=True)[:top_processes]),
    )


def is_process_alive(name: str) -> bool:
    expected = name
    with os.scandir(PROC_ROOT) as entries:
        for entry in entries:
            if not entry.name.isascii() or not entry.name.isdigit():
                continue
            try:
                stat = Path(entry.path, "stat").read_text(encoding="utf-8", errors="replace")
                closing_paren = stat.rfind(")")
                if closing_paren < 0:
                    continue
                process_name = stat[stat.find("(") + 1:closing_paren]
                if process_name == expected:
                    return True
                command = Path(entry.path, "cmdline").read_bytes().split(b"\0", 1)[0]
                executable = os.path.basename(os.fsdecode(command))
                if executable == expected:
                    return True
            except (OSError, ValueError):
                continue
    return False


async def check_ping(host: str, timeout_seconds: int) -> bool | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(timeout_seconds), host,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None
    except OSError:
        return False
    try:
        return await asyncio.wait_for(process.wait(), timeout_seconds + 2) == 0
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        return False


async def get_external_ip(url: str | None, session: ClientSession) -> str | None:
    if url is None:
        return None
    try:
        async with session.get(url, timeout=ClientTimeout(total=5), allow_redirects=False) as response:
            if response.status != 200:
                return None
            body = (await response.content.read(64)).decode("ascii").strip()
        return str(ipaddress.ip_address(body))
    except (ClientError, OSError, UnicodeError, ValueError, TimeoutError):
        return None


async def get_docker_reading() -> DockerReading:
    if shutil.which("docker") is None:
        return DockerReading(available=False)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return DockerReading(available=True, error="Docker query failed")
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        return DockerReading(available=True, error="Docker query timed out")
    except OSError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        return DockerReading(available=True, error="Docker query failed")
    if process.returncode != 0:
        return DockerReading(available=True, error="Docker query failed")
    containers = []
    for line in output.decode("utf-8", errors="replace").splitlines()[:100]:
        name, separator, status = line.partition("\t")
        if separator:
            safe_name = _safe_text(name) if DOCKER_NAME_PATTERN.fullmatch(name) else "[invalid container name]"
            containers.append((safe_name, _safe_text(status)))
    return DockerReading(available=True, containers=tuple(containers))


def read_log_tail(path: Path, allowed_directories: Sequence[Path], lines: int) -> list[str]:
    resolved_path = path.resolve(strict=True)
    descriptor = os.open(
        resolved_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened_path = Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
        if not any(opened_path.is_relative_to(directory) for directory in allowed_directories):
            raise PermissionError("Log path is outside the configured directories")
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Log path is not a regular file")
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        with stream as file:
            position = file.seek(0, os.SEEK_END)
            content = bytearray()
            while position > 0 and content.count(b"\n") <= lines and len(content) < MAX_LOG_BYTES:
                chunk_start = max(0, position - 65536)
                file.seek(chunk_start)
                content[:0] = file.read(position - chunk_start)
                position = chunk_start
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return bytes(content).decode("utf-8", errors="replace").splitlines()[-lines:]
