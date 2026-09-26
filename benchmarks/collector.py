"""Process-scope collection for the YTM benchmark (handout sections 4-7, 15).

Standard library only. A dedicated cgroup v2 scope is the authoritative
process membership; /proc provides per-process RSS/PSS/CPU diagnostics.

The collector runs outside the measured scope, so its own footprint is
reported separately and never subtracted from the target.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
CLK_TCK = os.sysconf("SC_CLK_TCK")
CGROUP_ROOT = Path("/sys/fs/cgroup")
MY_CGROUP = Path(
    "/sys/fs/cgroup/user.slice/user-%d.slice/user@%d.service" % (os.getuid(), os.getuid())
)


def now_ns() -> int:
    return time.monotonic_ns()


def read_text(path) -> str | None:
    try:
        return Path(path).read_bytes().decode("utf-8", "replace")
    except OSError:
        return None


def run_capture(cmd, timeout=5.0) -> str | None:
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, text=True,
            env={**os.environ, "LC_ALL": "C"},  # stable, dot-decimal tool output
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


# --------------------------------------------------------------------------
# cgroup v2 target scope
# --------------------------------------------------------------------------

def find_delegated_base() -> Path:
    """The nearest writable cgroup v2 directory with memory+cpu controllers."""
    candidates: list[Path] = []
    own = None
    line = read_text("/proc/self/cgroup") or ""
    for entry in line.splitlines():
        if entry.startswith("0::"):
            own = CGROUP_ROOT / entry[3:].lstrip("/")
    if MY_CGROUP.is_dir():
        candidates.append(MY_CGROUP)
    if own is not None:
        cursor = own
        while cursor != CGROUP_ROOT and cursor.parent != cursor:
            candidates.append(cursor)
            cursor = cursor.parent
    for candidate in candidates:
        try:
            controllers = (candidate / "cgroup.controllers").read_text().split()
        except OSError:
            continue
        if "memory" in controllers and "cpu" in controllers and os.access(candidate, os.W_OK):
            return candidate
    raise RuntimeError(
        "no writable delegated cgroup v2 directory with memory+cpu controllers; "
        "cannot guarantee process containment"
    )


class Scope:
    """One fresh cgroup v2 directory used as the target scope for a session."""

    def __init__(self, base: Path, name: str):
        self.name = name
        self.path = base / name
        self.path.mkdir()
        controllers = (self.path / "cgroup.controllers").read_text().split()
        missing = [c for c in ("memory", "cpu") if c not in controllers]
        if missing:
            self.remove(force=True)
            raise RuntimeError(f"{self.path}: controllers unavailable: {missing}")

    # -- membership -----------------------------------------------------

    def procs(self) -> set[int]:
        found: set[int] = set()
        stack = [self.path]
        while stack:
            directory = stack.pop()
            data = read_text(directory / "cgroup.procs")
            if data:
                found.update(int(x) for x in data.split())
            try:
                stack.extend(p for p in directory.iterdir() if p.is_dir())
            except OSError:
                pass
        return found

    def move_self_into(self) -> None:
        (self.path / "cgroup.procs").write_text(str(os.getpid()))

    def move_pid(self, pid: int) -> None:
        (self.path / "cgroup.procs").write_text(str(pid))

    # -- counters -------------------------------------------------------

    def _number(self, file: str, key: str | None = None):
        data = read_text(self.path / file)
        if data is None:
            return None
        if key is None:
            try:
                return int(data.strip())
            except ValueError:
                return None
        for line in data.splitlines():
            fields = line.split()
            if fields and fields[0] == key and len(fields) > 1:
                return int(fields[1])
        return None

    def cpu_usec(self):
        return self._number("cpu.stat", "usage_usec")

    def memory_current(self):
        return self._number("memory.current")

    def memory_peak(self):
        return self._number("memory.peak")

    def pids_current(self):
        return self._number("pids.current")

    def populated(self) -> bool:
        return bool(self.procs())

    # -- teardown -------------------------------------------------------

    def await_empty(self, timeout=15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.populated():
                return True
            time.sleep(0.05)
        return not self.populated()

    def identity_of(self, pid: int):
        try:
            rest = read_text(f"/proc/{pid}/stat")
        except OSError:
            return None
        if rest is None:
            return None
        return rest.rsplit(")", 1)[1].split()[19]

    def kill_members(self, sig) -> None:
        for pid in list(self.procs()):
            started = self.identity_of(pid)
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue

    def remove(self, force=False) -> None:
        if force:
            self.kill_members(9)
            self.await_empty(3.0)
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.await_empty(10.0):
            self.remove()
        else:
            self.kill_members(9)
            self.await_empty(5.0)
            self.remove(force=True)


# --------------------------------------------------------------------------
# /proc per-process readers
# --------------------------------------------------------------------------

def read_stat(pid: int):
    data = read_text(f"/proc/{pid}/stat")
    if data is None:
        return None
    try:
        rest = data.rsplit(")", 1)[1].split()
        return {
            "state": rest[0],
            "ppid": int(rest[1]),
            "utime_ticks": int(rest[11]),
            "stime_ticks": int(rest[12]),
            "threads": int(rest[17]),
            "starttime_ticks": int(rest[19]),
        }
    except (IndexError, ValueError):
        return None


def read_rss_bytes(pid: int):
    data = read_text(f"/proc/{pid}/statm")
    if data is None:
        return None
    try:
        return int(data.split()[1]) * PAGE_SIZE
    except (IndexError, ValueError):
        return None


def read_status(pid: int):
    data = read_text(f"/proc/{pid}/status")
    if data is None:
        return None
    out = {}
    for line in data.splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if key in ("VmRSS", "VmHWM", "VmPeak", "Threads") and value:
            try:
                out[key] = int(value.split()[0]) * 1024
            except (ValueError, IndexError):
                pass
    return out


def read_smaps_rollup(pid: int):
    """PSS and friends, or (None, reason) when unavailable."""
    data = read_text(f"/proc/{pid}/smaps_rollup")
    if data is None:
        try:
            os.stat(f"/proc/{pid}")
        except OSError:
            return None, "exited"
        return None, "permission-or-unsupported"
    out = {}
    for line in data.splitlines():
        key, _, value = line.partition(":")
        if key in ("Rss", "Pss", "Pss_Anon", "Pss_File", "Pss_Shmem") and value.strip():
            try:
                out[key] = int(value.strip().split()[0]) * 1024
            except (ValueError, IndexError):
                pass
    if "Pss" not in out:
        return None, "field-missing"
    return out, None


def classify(pid: int, root_pid: int, cache: dict):
    """Component + diagnostic tags for one PID, from exe/comm/argv."""
    stat = read_stat(pid)
    if stat is None:
        return None, None, None
    key = (pid, stat["starttime_ticks"])
    if key in cache:
        return cache[key]
    exe = os.readlink(f"/proc/{pid}/exe") if os.path.exists(f"/proc/{pid}/exe") else ""
    comm = ""
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        pass
    argv = []
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a][:12]
    except OSError:
        pass
    base = os.path.basename(exe) or comm

    component = "helpers"
    tags = set()
    if pid == root_pid:
        component = "ytm_main"
        tags.add("ytm")
    elif base == "mpv" or comm == "mpv":
        component = "mpv"
    if base.startswith("python"):
        tags.add("python")
    if any("yt-dlp" in a or "yt_dlp" in a for a in argv) or "yt-dlp" in base:
        tags.add("resolver")
    if any(a == "radio" for a in argv):
        tags.add("radio")
    if base in ("node", "deno", "bun") or any(os.path.basename(a) in ("node", "deno", "bun") for a in argv):
        tags.add("js")
    if base in ("wpctl", "pactl", "pamixer", "amixer"):
        tags.add("mixer")
    if component == "helpers" and not tags:
        tags.add("unknown")
    result = (component, sorted(tags), base)
    cache[key] = result
    return result


# --------------------------------------------------------------------------
# sampler
# --------------------------------------------------------------------------

class JsonlWriter:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, record: dict) -> None:
        self._file.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")

    def close(self) -> None:
        self._file.close()


class Registry:
    """Process identities seen in the scope, with sampled peaks and exits.

    Identity is (pid, start_ticks); a reused PID is a new identity and a
    PID that disappears is an exit, so last values are never carried forward.
    """

    def __init__(self, writer: JsonlWriter, run_id: str):
        self._writer = writer
        self._run_id = run_id
        self._known: dict[tuple[int, str], dict] = {}

    def see(self, pid, started, component, tags, exe, rss, vmhwm, pss, sample_ns):
        key = (pid, started)
        record = self._known.get(key)
        if record is None:
            record = {
                "schema_version": 1, "run_id": self._run_id, "event": "birth",
                "pid": pid, "start_ticks": started, "component": component,
                "tags": tags, "exe": exe, "first_seen_ns": sample_ns,
                "peak_rss_bytes": 0, "peak_pss_bytes": None,
                "vmhwm_bytes": None, "last_rss_bytes": None, "last_seen_ns": None,
            }
            self._known[key] = record
            self._writer.write(record)
        else:
            if record["component"] != component and component != "helpers":
                record["component"] = component
            record["last_rss_bytes"] = rss
            record["last_seen_ns"] = sample_ns
            record["peak_rss_bytes"] = max(record["peak_rss_bytes"] or 0, rss or 0)
            if vmhwm:
                record["vmhwm_bytes"] = max(record["vmhwm_bytes"] or 0, vmhwm)
            if pss is not None:
                record["peak_pss_bytes"] = max(record["peak_pss_bytes"] or 0, pss)

    def sweep(self, alive: set[tuple[int, str]], sample_ns: int):
        for key in list(self._known):
            if key in alive:
                continue
            record = self._known.pop(key)
            self._writer.write({
                "schema_version": 1, "run_id": self._run_id, "event": "exit",
                **_public_identity(record), "last_seen_ns": record["last_seen_ns"],
                "exit_seen_ns": sample_ns,
                "last_rss_bytes": record["last_rss_bytes"],
                "peak_rss_bytes": record["peak_rss_bytes"],
                "peak_pss_bytes": record["peak_pss_bytes"],
                "vmhwm_bytes": record["vmhwm_bytes"],
            })

    def alive_keys(self) -> set[tuple[int, str]]:
        return set(self._known)


def _public_identity(record: dict) -> dict:
    return {k: record[k] for k in ("pid", "start_ticks", "component", "tags", "exe")}


class Sampler:
    """100 ms RSS snapshots and 1 s PSS snapshots for one target scope."""

    def __init__(self, scope: Scope, root_pid_getter, writer, registry,
                 run_id, phase_getter, sample_ms=100, pss_ms=1000, session_name=None):
        self.scope = scope
        self.root_pid_getter = root_pid_getter
        self.writer = writer
        self.registry = registry
        self.run_id = run_id
        self.phase_getter = phase_getter
        self.session_name = session_name
        self.sample_ns = max(20, int(sample_ms)) * 1_000_000
        self.pss_ns = max(self.sample_ns, int(pss_ms) * 1_000_000)
        self._next_rss = 0
        self._next_pss = 0
        self._classify_cache: dict = {}
        self._sample_id = 0
        self.last: dict | None = None

    def tick(self, force_pss=False) -> dict | None:
        now = now_ns()
        if now >= self._next_pss or force_pss:
            self._next_pss = now + self.pss_ns
            self._next_rss = now + self.sample_ns
            return self.sample(kind="pss", now=now)
        if now >= self._next_rss:
            self._next_rss = now + self.sample_ns
            return self.sample(kind="rss", now=now)
        return None

    # -- one snapshot ----------------------------------------------------

    def sample(self, kind="rss", now=None) -> dict:
        start = now or now_ns()
        root_pid = self.root_pid_getter()
        pids = sorted(self.scope.procs())
        per_process = []
        errors: list[dict] = []
        rss = {"ytm_main": 0, "mpv": 0, "helpers": 0, "total": 0}
        pss = {"ytm_main": 0, "mpv": 0, "helpers": 0, "total": 0}
        pss_missing = {"ytm_main": 0, "mpv": 0, "helpers": 0}
        rss_missing = {"ytm_main": 0, "mpv": 0, "helpers": 0}
        alive: set[tuple[int, str]] = set()
        pss_parts: dict[str, list[int]] = {"ytm_main": [], "mpv": [], "helpers": []}
        for pid in pids:
            stat = read_stat(pid)
            if stat is None:
                continue  # exited between membership read and now
            started = stat["starttime_ticks"]
            component, tags, exe = classify(pid, root_pid, self._classify_cache)
            if component is None:
                component, tags, exe = "helpers", ["unknown"], ""
            rss_value = read_rss_bytes(pid)
            if rss_value is None:
                rss_value = 0
                rss_missing[component] = rss_missing.get(component, 0) + 1
                errors.append({"pid": pid, "why": "rss-unreadable"})
            vmhwm = None
            if kind == "pss" or (self._sample_id % 10 == 0):
                status = read_status(pid)
                if status:
                    vmhwm = status.get("VmHWM")
                    kernel_rss = status.get("VmRSS")
                    if kernel_rss is not None and rss_value:
                        # cross-check the fast statm source once a second
                        drift = abs(kernel_rss - rss_value)
                        if drift > max(1 << 20, kernel_rss // 20):
                            errors.append({
                                "pid": pid, "why": "statm-vs-status-drift",
                                "statm": rss_value, "status": kernel_rss,
                            })
            pss_value = None
            if kind == "pss":
                rollup, reason = read_smaps_rollup(pid)
                if rollup is None:
                    pss_missing[component] += 1
                    if reason not in ("exited",):
                        errors.append({"pid": pid, "why": f"pss-{reason}"})
                else:
                    pss_value = rollup.get("Pss")
            key = (pid, started)
            alive.add(key)
            self.registry.see(pid, started, component, tags, exe,
                              rss_value, vmhwm, pss_value, start)
            cpu_seconds = (stat["utime_ticks"] + stat["stime_ticks"]) / CLK_TCK
            rss[component] += rss_value
            rss["total"] += rss_value
            if pss_value is not None:
                pss[component] += pss_value
                pss["total"] += pss_value
                pss_parts[component].append(pss_value)
            entry = {
                "pid": pid, "start_ticks": started, "component": component,
                "tags": tags, "rss_bytes": rss_value, "cpu_seconds": round(cpu_seconds, 3),
            }
            if pss_value is not None:
                entry["pss_bytes"] = pss_value
            if vmhwm is not None:
                entry["vmhwm_bytes"] = vmhwm
            per_process.append(entry)
        self.registry.sweep(alive, start)

        collector_stat = read_stat(os.getpid())
        collector_rss = read_rss_bytes(os.getpid())
        collector_cpu = None
        if collector_stat:
            collector_cpu = (collector_stat["utime_ticks"] + collector_stat["stime_ticks"]) / CLK_TCK

        end = now_ns()
        sample = {
            "schema_version": 1,
            "run_id": self.run_id,
            "session": self.session_name,
            "sample_id": self._sample_id,
            "kind": kind,
            "monotonic_start_ns": start,
            "monotonic_end_ns": end,
            "read_duration_ns": end - start,
            "phases": self.phase_getter(),
            "membership_mode": "cgroup-v2-scope",
            "identities_seen": len(pids),
            "identities_read": len(per_process),
            "completeness": "complete" if not rss_missing["ytm_main"] + rss_missing["mpv"] + rss_missing["helpers"] else "partial",
            "rss_bytes": rss,
            "pss_bytes": ({**pss} if kind == "pss" and not any(pss_missing.values()) else None) if kind == "pss" else None,
            "pss_coverage": (
                {"missing": sum(pss_missing.values()), "by_component": pss_missing}
                if kind == "pss" else None
            ),
            "cgroup_cpu_usec": self.scope.cpu_usec(),
            "cgroup_memory_current_bytes": self.scope.memory_current(),
            "cgroup_memory_peak_bytes": self.scope.memory_peak(),
            "cgroup_pids": self.scope.pids_current(),
            "per_process": per_process,
            "read_errors": errors,
            "collector": {
                "rss_bytes": collector_rss,
                "cpu_seconds": collector_cpu,
            },
        }
        self._sample_id += 1
        self.writer.write(sample)
        self.last = sample
        return sample


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------

def _kv_file(path: Path) -> dict:
    out = {}
    data = read_text(path)
    if not data:
        return out
    for line in data.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
        else:
            key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"')
    return out


def _git(repo: Path, *args: str):
    return run_capture(["git", "-C", str(repo), *args], timeout=10.0)


def collect_manifest(repo: Path, run_id: str, workload_hash: str, options: dict) -> dict:
    os_release = _kv_file(Path("/etc/os-release"))
    cpuinfo = read_text("/proc/cpuinfo") or ""
    model = next((l.split(":", 1)[1].strip() for l in cpuinfo.splitlines()
                  if l.startswith("model name")), "")
    meminfo = _kv_file(Path("/proc/meminfo"))
    governor = read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    power = {}
    for supply in sorted(Path("/sys/class/power_supply").glob("*")):
        try:
            kind = (supply / "type").read_text().strip()
            state = {"type": kind}
            if (supply / "online").exists():
                state["online"] = (supply / "online").read_text().strip()
            if (supply / "status").exists():
                state["status"] = (supply / "status").read_text().strip()
            if (supply / "capacity").exists():
                state["capacity_percent"] = (supply / "capacity").read_text().strip()
            power[supply.name] = state
        except OSError:
            continue
    platform_profile = read_text("/sys/firmware/acpi/platform_profile")
    thermal = {}
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            name = (zone / "type").read_text().strip()
            temp = (zone / "temp").read_text().strip()
            thermal[name] = int(temp) / 1000.0
        except (OSError, ValueError):
            continue

    audio = {}
    pactl = run_capture(["pactl", "info"], timeout=5.0)
    if pactl:
        for line in pactl.splitlines():
            key, _, value = line.partition(":")
            if key in ("Server Name", "Server Version", "Default Sink"):
                audio[key] = value.strip()
    sinks = run_capture(["pactl", "list", "short", "sinks"], timeout=5.0)
    if sinks:
        audio["sinks"] = sinks.splitlines()

    route = run_capture(["ip", "route", "get", "1.1.1.1"], timeout=5.0)
    iface = None
    if route:
        match = re.search(r"\bdev (\S+)", route)
        iface = match.group(1) if match else None
    network = {"route": route}
    if iface:
        network["interface"] = iface
        network["wireless"] = (Path(f"/sys/class/net/{iface}/wireless").exists()
                               if Path(f"/sys/class/net/{iface}").exists() else None)

    deps = {}
    for name in ("textual", "ytmusicapi", "requests", "yt-dlp", "rich", "pygments"):
        try:
            from importlib import metadata
            deps[name] = metadata.version(name)
        except Exception:
            deps[name] = None
    try:
        from importlib import metadata
        ytm_version = metadata.version("ytm")
    except Exception:
        ytm_version = None
    repo_version = None
    try:
        import tomllib
        pyproject = repo / "pyproject.toml"
        if pyproject.exists():
            repo_version = (tomllib.loads(pyproject.read_text()).get("project") or {}).get("version")
    except Exception:
        repo_version = None

    mpv_version = run_capture(["mpv", "--version"], timeout=5.0)
    ytdlp = repo / ".venv" / "bin" / "yt-dlp"
    ytdlp_version = run_capture([str(ytdlp), "--version"], timeout=5.0) if ytdlp.exists() else None

    pot_url = None
    try:
        import tomllib
        cfg = Path.home() / ".config" / "ytm" / "config.toml"
        if cfg.exists():
            parsed = tomllib.loads(cfg.read_text())
            pot_url = (parsed.get("pot") or {}).get("base_url")
    except Exception:
        pass
    pot_reachable = False
    if pot_url:
        try:
            host_port = pot_url.split("//", 1)[-1].split("/", 1)[0]
            host, _, port = host_port.partition(":")
            with socket.create_connection((host, int(port or 80)), timeout=1.0):
                pot_reachable = True
        except OSError:
            pot_reachable = False

    return {
        "schema_version": 1,
        "run_id": run_id,
        "kind": "manifest",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "monotonic_clock": "CLOCK_MONOTONIC (Linux, system-wide)",
        "options": options,
        "workload_hash": workload_hash,
        "machine": {
            "hostname": os.uname().nodename,
            "os": os_release.get("PRETTY_NAME") or os_release.get("NAME"),
            "kernel": os.uname().release,
            "architecture": os.uname().machine,
            "cpu_model": model,
            "logical_cpus": os.cpu_count(),
            "affinity": sorted(os.sched_getaffinity(0)),
            "governor": governor,
            "mem_total_kib": meminfo.get("MemTotal"),
            "mem_available_kib": meminfo.get("MemAvailable"),
            "swap_total_kib": meminfo.get("SwapTotal"),
            "power": power,
            "platform_profile": platform_profile,
            "thermal_c": thermal,
        },
        "runtime": {
            "python_version": __import__("sys").version,
            "python_executable": __import__("sys").executable,
            "ytm_version": ytm_version,
            "ytm_repo_version": repo_version,
            "install_mode": ("editable" if (repo / ".venv" / "lib").exists()
                             and any((repo / ".venv" / "lib").glob("python*/site-packages/__editable__*"))
                             else "unknown"),
            "dependencies": deps,
            "mpv_version": (mpv_version or "").splitlines()[0] if mpv_version else None,
            "ytdlp_version": ytdlp_version,
            "node": run_capture(["node", "--version"], timeout=5.0),
        },
        "source_revision": {
            "commit": _git(repo, "rev-parse", "HEAD"),
            "dirty_files": len((_git(repo, "status", "--porcelain") or "").splitlines()),
            "describe": _git(repo, "describe", "--tags", "--always", "--dirty"),
        },
        "audio": audio,
        "network": network,
        "pot_provider": {"url": pot_url or "default-http://127.0.0.1:4416", "reachable": pot_reachable},
        "authentication": "staged-copy of operator's auth files into private home; values never recorded",
        "cgroup": {
            "version": 2,
            "base": str(find_delegated_base()),
        },
    }
