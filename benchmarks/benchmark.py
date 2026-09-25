#!/usr/bin/env python3
"""YTM full-process benchmark runner (handout: docs/BENCHMARK_IMPLEMENTATION_HANDOUT.md).

Produces machine-readable evidence in benchmarks/results/<run-id>/ and a
generated report. Standard library only; the measured target is the real
console entry point in a real PTY.

Commands:
  python benchmarks/benchmark.py preflight
  python benchmarks/benchmark.py run --profile smoke|public|endurance|full
  python benchmarks/benchmark.py footprint
  python benchmarks/benchmark.py report benchmarks/results/<run-id>
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parent

from collector import (  # noqa: E402
    JsonlWriter, Registry, Sampler, Scope, collect_manifest, find_delegated_base,
    now_ns, read_smaps_rollup, read_text, run_capture,
)
from driver import Session  # noqa: E402
from reporting import generate  # noqa: E402

RESULTS_DIR = HERE / "results"
REAL_CONFIG = Path.home() / ".config" / "ytm"
AUTH_FILES = ("auth.json", "auth.source.json", "cookies.txt",
              "oauth_client.json", "oauth_desktop_client.json")
MIB = 1_048_576

PROFILES = {
    "smoke": {"startup_runs": 2, "search_runs": 4, "switches": 3, "endurance_minutes": 0},
    "public": {"startup_runs": 10, "search_runs": 50, "switches": 30, "endurance_minutes": 0},
    "endurance": {"startup_runs": 0, "search_runs": 0, "switches": 0,
                  "endurance_minutes": 30, "longevity_tracks": 50},
    "full": {"startup_runs": 10, "search_runs": 50, "switches": 30,
             "endurance_minutes": 30, "longevity_tracks": 50},
}


def default_executable() -> str:
    candidate = shutil.which("ytm")
    if candidate:
        return candidate
    return str(REPO / ".venv" / "bin" / "ytm")


def dir_usage(path: Path) -> dict:
    apparent = allocated = files = 0
    if not path.exists():
        return {"apparent_bytes": 0, "allocated_bytes": 0, "files": 0}
    for root, dirs, names in os.walk(path, followlinks=False):
        for name in names:
            full = Path(root) / name
            try:
                stat = os.lstat(full)
            except OSError:
                continue
            files += 1
            apparent += stat.st_size
            allocated += stat.st_blocks * 512
    return {"apparent_bytes": apparent, "allocated_bytes": allocated, "files": files}


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def tcp_reachable(host: str, port: int, timeout=2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Bench:
    def __init__(self, args):
        self.args = args
        self.started = time.monotonic()
        self.run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + os.urandom(3).hex()
        self.run_dir = Path(args.output).resolve() / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.private = self.run_dir / "private"
        self.home = self.private / "home"
        self.workload = json.loads(Path(args.scenario_file).read_text())
        self.workload_hash = hashlib.sha256(
            json.dumps(self.workload, sort_keys=True).encode()).hexdigest()
        self.events = JsonlWriter(self.run_dir / "events.jsonl")
        self.samples = JsonlWriter(self.run_dir / "samples.jsonl")
        self.processes = JsonlWriter(self.run_dir / "processes.jsonl")
        self.trials = JsonlWriter(self.run_dir / "trials.jsonl")
        self.sessions: list[Session] = []
        self.samplers: list[Sampler] = []
        self.phases: list[str] = []
        self.scopes: list[Scope] = []
        self.executable = args.ytm_executable or default_executable()
        self.terminal = tuple(int(x) for x in args.terminal_size.split("x"))  # rows x cols
        self.cache_before = None
        self.manifest: dict = {}
        self.profile_values: dict = {}

    # -- plumbing ---------------------------------------------------------

    def log(self, message: str) -> None:
        elapsed = time.monotonic() - self.started
        print(f"[{elapsed:7.1f}s] {message}", flush=True)

    def session_dir(self, name: str) -> Path:
        return self.run_dir / "sessions" / name

    def event(self, name: str, **fields) -> None:
        self.events.write({"schema_version": 1, "run_id": self.run_id,
                           "event": name, **fields})

    def begin_phase(self, phase: str) -> None:
        self.phases.append(phase)
        self.event("phase_begin", phase=phase, monotonic_ns=now_ns(),
                   session=self._current_session_name())

    def end_phase(self, phase: str) -> None:
        if phase in self.phases:
            self.phases.remove(phase)
        self.event("phase_end", phase=phase, monotonic_ns=now_ns(),
                   session=self._current_session_name())

    def _current_session_name(self):
        for session in reversed(self.sessions):
            if session.master is not None:
                return session.name
        return self.sessions[-1].name if self.sessions else None

    def phase_names(self):
        return list(self.phases)

    def record_pty(self, session: str, data: bytes) -> None:
        pass

    def pump(self) -> None:
        for session in list(self.sessions):
            session.poll()
        for sampler in list(self.samplers):
            sampler.tick()

    def sleep_phase(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump()
            time.sleep(0.02)

    # -- profile staging ---------------------------------------------------

    def stage_profile(self) -> dict:
        (self.home / ".config" / "ytm").mkdir(parents=True, exist_ok=True)
        copied = []
        for name in AUTH_FILES:
            source = REAL_CONFIG / name
            if source.exists():
                target = self.home / ".config" / "ytm" / name
                shutil.copy2(source, target)
                os.chmod(target, 0o600)
                copied.append(name)
        control = {"staged_files": copied}
        real_config = REAL_CONFIG / "config.toml"
        if real_config.exists():
            try:
                parsed = tomllib.loads(real_config.read_text())
            except tomllib.TOMLDecodeError:
                parsed = {}
            if (parsed.get("update") or {}).get("auto"):
                (self.home / ".config" / "ytm" / "config.toml").write_text(
                    "[update]\nauto = false\n# benchmark control: user config carried over partially\n")
                control["update_auto_forced_off"] = True
            else:
                shutil.copy2(real_config, self.home / ".config" / "ytm" / "config.toml")
                control["config_copied"] = True
        return control

    def restage_auth(self) -> None:
        """Refresh staged credentials from the operator's real config.

        Google rotates browser session tokens; a copy staged once can go
        stale mid-run. Only credentials are refreshed, never config edits.
        """
        target_dir = self.home / ".config" / "ytm"
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in AUTH_FILES:
            source = REAL_CONFIG / name
            if not source.exists():
                continue
            target = target_dir / name
            try:
                shutil.copy2(source, target)
                os.chmod(target, 0o600)
            except OSError:
                pass

    def make_env(self, session_dir: Path) -> dict:
        env = dict(os.environ)
        env.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "YTM_TUI_LOG": str(session_dir / "tui.log"),
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        })
        for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "PULSE_SERVER",
                    "PIPEWIRE_REMOTE", "DISPLAY", "WAYLAND_DISPLAY"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        return env

    def workload_sequence(self, count: int) -> list[dict]:
        queries = self.workload["queries"]
        rng = random.Random(self.args.seed)
        base = [dict(q) for q in queries if q.get("cache") != "hit"]
        repeats = [dict(q) for q in queries if q.get("cache") == "hit"]
        sequence: list[dict] = []
        while len(sequence) < count:
            batch = base + [dict(rng.choice(repeats)) for _ in repeats] if repeats else base
            rng.shuffle(batch)
            sequence.extend(batch)
        for index, item in enumerate(sequence):
            item["trial_index"] = index
        return sequence[:count]

    # -- session management -------------------------------------------------

    def new_session(self, scope: Scope, name: str) -> Session:
        self.restage_auth()
        session_dir = self.session_dir(name)
        session_dir.mkdir(parents=True, exist_ok=True)
        session = Session(self, name, scope, self.executable,
                          self.make_env(session_dir), str(REPO), self.terminal)
        self.sessions.append(session)
        return session

    def attach_sampler(self, session: Session, scope: Scope) -> Sampler:
        sampler = Sampler(
            scope, lambda: session.pid,
            self.samples, Registry(self.processes, self.run_id), self.run_id,
            self.phase_names, sample_ms=self.args.sample_ms, pss_ms=self.args.pss_ms,
            session_name=session.name,
        )
        self.samplers.append(sampler)
        return sampler

    def detach_sampler(self, sampler: Sampler) -> None:
        if sampler in self.samplers:
            self.samplers.remove(sampler)

    def write_trial(self, record: dict) -> None:
        record.setdefault("schema_version", 1)
        record.setdefault("run_id", self.run_id)
        record.setdefault("monotonic_ns", now_ns())
        self.trials.write(record)

    # -- scenarios ----------------------------------------------------------

    def startup_trial(self, name: str, session_name: str | None = None) -> dict:
        scope = Scope(self.cgroup_base, f"startup-{name}")
        self.scopes.append(scope)
        session = self.new_session(scope, session_name or f"startup-{name}")
        sampler = self.attach_sampler(session, scope)
        trial = {"kind": "startup", "scenario": "startup", "session": session.name,
                 "trial": name, "success": False}
        try:
            launch_ns = session.launch()
            trial["monotonic_ns"] = launch_ns
            ready = session.ready()
            trial.update({
                "success": True,
                "ready_ns": ready["ready_ns"],
                "ready_ms": (ready["ready_ns"] - launch_ns) / 1e6,
                "ready_focus": ready["focus"],
            })
            self.event("ui.ready", session=session.name, monotonic_ns=ready["ready_ns"],
                       fields={"method": "input round-trip (escape accepted, focus moved)"})
            quiet = session.wait_quiet(1.5, 30.0)
            trial["background_quiet"] = quiet
        except TimeoutError as exc:
            trial["error"] = str(exc)
        finally:
            shutdown = session.quit()
            trial["shutdown_clean"] = shutdown["clean"]
            trial["scope_empty"] = shutdown["scope_empty"]
            self.detach_sampler(sampler)
            session.close()
            self.scopes.remove(scope)
            if not shutdown["scope_empty"] or not scope.await_empty(5.0):
                scope.kill_members(9)
                scope.await_empty(5.0)
            scope.remove(force=True)
        self.write_trial(trial)
        return trial

    def run_startup_trials(self, count: int) -> None:
        for index in range(count):
            trial = self.startup_trial(f"{index + 1:02d}")
            self.log(f"startup {index + 1}/{count}: "
                     f"{'ok' if trial['success'] else 'FAILED'} "
                     f"ready={trial.get('ready_ms', float('nan')):.0f}ms")
            time.sleep(self.args.inter_run_gap)

    def wait_position(self, session: Session, seconds: float, timeout: float) -> bool:
        def check():
            value = session.mpv.get("time-pos") if session.mpv else None
            if isinstance(value, (int, float)) and value >= seconds:
                return True
            events = session.mpv.events_since(session.last_write_ns or 0) if session.mpv else []
            if any(e.get("event") == "end-file" for e in events):
                return True
            return None
        try:
            session.pump_until(check, timeout, f"position>={seconds}")
            return True
        except TimeoutError:
            return False

    def measure_switch(self, session: Session, direction: str) -> dict:
        trial = {"kind": "switch", "scenario": f"manual-{direction}",
                 "session": session.name, "direction": direction, "success": False}
        try:
            result = session.switch(direction)
            trial.update(result)
            trial["success"] = True
            trial["video_id"] = _video_id(result.get("path"))
        except TimeoutError as exc:
            trial["error"] = str(exc)
        self.write_trial(trial)
        return trial

    def playback_recovery(self, session: Session, query: str) -> bool:
        """Re-seed playback after a failure: search, focus results, play first."""
        try:
            session.search_trial(query, timeout=30.0)
            result = session.play_first(timeout=60.0)
            self.write_trial({"kind": "playback", "scenario": "recovery",
                              "session": session.name, "query": query,
                              "success": True, **_sanitize_playback(result)})
            return True
        except TimeoutError as exc:
            self.write_trial({"kind": "playback", "scenario": "recovery",
                              "session": session.name, "query": query,
                              "success": False, "error": str(exc)})
            return False

    def run_interactive(self, profile: dict) -> None:
        scope = Scope(self.cgroup_base, "interactive")
        self.scopes.append(scope)
        session = self.new_session(scope, "interactive")
        sampler = self.attach_sampler(session, scope)
        try:
            launch_ns = session.launch()
            ready = session.ready()
            self.write_trial({
                "kind": "startup", "scenario": "interactive-startup",
                "session": session.name, "monotonic_ns": launch_ns,
                "ready_ns": ready["ready_ns"],
                "ready_ms": (ready["ready_ns"] - launch_ns) / 1e6, "success": True,
            })
            self.log(f"interactive: ready in {(ready['ready_ns'] - launch_ns) / 1e6:.0f}ms")
            if session.wait_quiet(2.0, 30.0):
                self.event("ui.background_ready", session=session.name, monotonic_ns=now_ns())
            self.log("interactive: idle window 30 s")
            self.begin_phase("idle")
            self.sleep_phase(30.0)
            self.end_phase("idle")

            self.log(f"interactive: live search trials ({profile['search_runs']})")
            self.begin_phase("search")
            first_uncached = True
            for item in self.workload_sequence(profile["search_runs"]):
                trial = {"kind": "search", "scenario": "live-search",
                         "session": session.name, "query_id": item["id"],
                         "query": item["text"], "cache_expectation": item.get("cache"),
                         "success": False}
                try:
                    result = session.search_trial(item["text"])
                    trial.update(result)
                    trial["success"] = True
                except TimeoutError as exc:
                    trial["error"] = str(exc)
                self.write_trial(trial)
                if trial["success"] and result["cache_class"] == "miss" and first_uncached:
                    first_uncached = False
                    self.begin_phase("search-results")
                    self.sleep_phase(3.0)
                    self.end_phase("search-results")
                self.sleep_phase(0.2)
            self.end_phase("search")
            self.log(f"interactive: searches done ({profile['search_runs']})")

            self.log("interactive: first playback")
            self.begin_phase("playback")
            pending = {"kind": "playback", "scenario": "submit-while-search-pending",
                       "session": session.name, "success": False}
            item = self.workload["queries"][0]
            pending["query"] = item["text"]
            try:
                session.focus_search()
                previous_path = session.mpv.get("path") if session.mpv else None
                session.set_query(item["text"])
                index = session.trace_index
                t0 = session.send_special(b"\r")
                completion = session.trace_wait(
                    lambda r: r["kind"] in ("request_done", "request_failed")
                    and r["cmd"] == "search", 40.0, "pending search completion",
                    since_index=index)
                pending["search_elapsed_ms"] = (completion["receipt_ns"] - t0) / 1e6
                result = session.wait_playback(previous_path, t0)
                pending.update(_sanitize_playback(result))
                pending["success"] = True
                session.last_write_ns = t0
            except TimeoutError as exc:
                pending["error"] = str(exc)
            self.write_trial(pending)
            if not pending["success"]:
                self.playback_recovery(session, item["text"])
            self.end_phase("playback")

            self.log("interactive: steady playback window (30-60 s after start)")
            self.begin_phase("steady-wait")
            self.sleep_phase(30.0)
            self.end_phase("steady-wait")
            self.begin_phase("steady")
            self.sleep_phase(30.0)
            self.end_phase("steady")

            self.log(f"interactive: switching trials ({profile['switches']})")
            self.begin_phase("switching")
            successes = 0
            for index in range(profile["switches"]):
                self.wait_position(session, self.args.dwell_seconds, 180.0)
                direction = "next"
                chrome = index % 6
                if chrome == 4:
                    direction = "prev" if (session.mpv.get("playlist-pos") or 0) > 0 else "next"
                if chrome == 5 and index > 0:
                    # select another search result: refresh results, then row 1
                    try:
                        session.search_trial(self.workload["queries"][1]["text"])
                        session.focus_results()
                        session.send(b"\x1b[B")  # down: row 0 -> row 1
                        time.sleep(0.2)
                        previous_path = session.mpv.get("path") if session.mpv else None
                        previous_pos = session.mpv.get("playlist-pos") if session.mpv else None
                        t0 = session.send(b"\r")
                        result = session.wait_playback(previous_path, t0)
                        self.write_trial({"kind": "switch", "scenario": "select-another-result",
                                          "session": session.name, "direction": "select-row",
                                          "selected_row": 1, "previous_playlist_pos": previous_pos,
                                          "success": True,
                                          **_sanitize_playback(result)})
                        successes += 1
                        continue
                    except TimeoutError as exc:
                        self.write_trial({"kind": "switch", "scenario": "select-another-result",
                                          "session": session.name, "direction": "select-row",
                                          "success": False, "error": str(exc)})
                trial = self.measure_switch(session, direction)
                if trial["success"]:
                    successes += 1
                elif not self.playback_recovery(session, item["text"]):
                    self.log("interactive: playback lost; abandoning switching trials")
                    break
            self.log(f"interactive: switches done ({successes}/{profile['switches']} ok)")
            self.end_phase("switching")

            self.begin_phase("final")
            self.sleep_phase(15.0)
            self.end_phase("final")
        finally:
            shutdown = session.quit()
            self.write_trial({"kind": "shutdown", "scenario": "interactive-shutdown",
                              "session": session.name, "success": shutdown["clean"],
                              "scope_empty": shutdown["scope_empty"],
                              "elapsed_ms": shutdown["elapsed_ms"]})
            self.detach_sampler(sampler)
            session.close()
            self.scopes.remove(scope)
            if not shutdown["scope_empty"] or not scope.await_empty(5.0):
                scope.kill_members(9)
                scope.await_empty(5.0)
            scope.remove(force=True)
            self.log("interactive: session closed")

    def run_endurance(self, profile: dict) -> None:
        minutes = profile["endurance_minutes"]
        tracks_wanted = profile.get("longevity_tracks", 50)
        scope = Scope(self.cgroup_base, "endurance")
        self.scopes.append(scope)
        session = self.new_session(scope, "endurance")
        sampler = self.attach_sampler(session, scope)
        query = self.workload["queries"][0]["text"]
        try:
            launch_ns = session.launch()
            deadline = time.monotonic() + minutes * 60.0
            ready = session.ready()
            self.write_trial({
                "kind": "startup", "scenario": "endurance-startup", "session": session.name,
                "monotonic_ns": launch_ns, "ready_ns": ready["ready_ns"],
                "ready_ms": (ready["ready_ns"] - launch_ns) / 1e6, "success": True})
            self.log(f"endurance: ready in {(ready['ready_ns'] - launch_ns) / 1e6:.0f}ms")
            session.wait_quiet(2.0, 30.0)

            self.begin_phase("longevity")
            try:
                session.search_trial(query, timeout=40.0)
                result = session.play_first()
                trial = {"kind": "playback", "scenario": "endurance-first",
                         "session": session.name, "query": query, "success": True,
                         **_sanitize_playback(result)}
            except TimeoutError as exc:
                trial = {"kind": "playback", "scenario": "endurance-first",
                         "session": session.name, "query": query, "success": False,
                         "error": str(exc)}
            self.write_trial(trial)
            tracks = 1 if trial["success"] else 0
            failures = 0 if trial["success"] else 1
            if not trial["success"]:
                # one recovery attempt before giving up on the advance loop
                if self.playback_recovery(session, query):
                    tracks = 1
                    failures = 0
            checkpoints = {1, 5, 10, 25, 50}
            self._checkpoint(session, tracks, checkpoints)
            dwell = max(self.args.dwell_seconds, 20.0)
            direction = "next"
            while tracks < tracks_wanted and time.monotonic() < deadline - 90:
                self.wait_position(session, dwell, 240.0)
                direction = "prev" if (direction == "next" and tracks % 7 == 0
                                       and (session.mpv.get("playlist-pos") or 0) > 0) else "next"
                result = self.measure_switch(session, direction)
                if result["success"]:
                    tracks += 1
                    if tracks % 5 == 0 or tracks in checkpoints:
                        self.log(f"endurance: track {tracks} reached "
                                 f"({(time.monotonic() - self.started) / 60:.1f} min elapsed)")
                    self._checkpoint(session, tracks, checkpoints)
                else:
                    failures += 1
                    if not self.playback_recovery(session, query):
                        self.log("endurance: playback lost; stopping advance loop")
                        break
                if tracks and tracks % 8 == 0:
                    self._endurance_search(session, tracks)
            tail_tick = 0
            while time.monotonic() < deadline - 45:
                self.sleep_phase(30.0)
                tail_tick += 1
                if tail_tick % 4 == 0:
                    self._endurance_search(session, tracks + tail_tick, quiet=True)
            self.log(f"endurance: advance loop done, tracks={tracks}, failures={failures}")
            self.event("endurance.summary", session=session.name, monotonic_ns=now_ns(),
                       fields={"tracks": tracks, "failures": failures})
            self.end_phase("longevity")
            self.begin_phase("final")
            self.sleep_phase(15.0)
            self.end_phase("final")
        finally:
            shutdown = session.quit()
            self.write_trial({"kind": "shutdown", "scenario": "endurance-shutdown",
                              "session": session.name, "success": shutdown["clean"],
                              "scope_empty": shutdown["scope_empty"],
                              "elapsed_ms": shutdown["elapsed_ms"]})
            self.detach_sampler(sampler)
            session.close()
            self.scopes.remove(scope)
            if not shutdown["scope_empty"] or not scope.await_empty(5.0):
                scope.kill_members(9)
                scope.await_empty(5.0)
            scope.remove(force=True)
            self.log("endurance: session closed")

    def _checkpoint(self, session: Session, tracks: int, checkpoints: set) -> None:
        if tracks not in checkpoints:
            return
        name = f"checkpoint-{tracks}"
        self.event("longevity.checkpoint", session=session.name, monotonic_ns=now_ns(),
                   fields={"tracks": tracks})
        self.begin_phase(name)
        self.sleep_phase(5.0)
        self.end_phase(name)

    def _endurance_search(self, session: Session, tracks: int, quiet=False) -> None:
        item = random.Random(self.args.seed + tracks).choice(self.workload["queries"])
        try:
            result = session.search_trial(item["text"], timeout=40.0)
            self.write_trial({"kind": "search", "scenario": "endurance-search",
                              "session": session.name, "query": item["text"],
                              "success": True, **result})
        except TimeoutError as exc:
            self.write_trial({"kind": "search", "scenario": "endurance-search",
                              "session": session.name, "query": item["text"],
                              "success": False, "error": str(exc)})
        session.focus_results()

    # -- preflight ----------------------------------------------------------

    def preflight(self, quick=False) -> dict:
        checks = []
        executable = Path(self.executable)
        checks.append({"check": "ytm-executable", "ok": executable.exists() and os.access(executable, os.X_OK),
                       "detail": str(executable)})
        checks.append({"check": "mpv", "ok": shutil.which("mpv") is not None,
                       "detail": shutil.which("mpv")})
        checks.append({"check": "pss-readable", "ok": read_smaps_rollup(os.getpid())[0] is not None,
                       "detail": "smaps_rollup on collector"})
        pactl = run_capture(["pactl", "info"], timeout=5.0)
        checks.append({"check": "audio-server", "ok": bool(pactl and "Default Sink" in pactl),
                       "detail": "pactl info"})
        checks.append({"check": "network-youtube", "ok": tcp_reachable("music.youtube.com", 443),
                       "detail": "TCP music.youtube.com:443"})
        pot = self.manifest.get("pot_provider", {})
        if pot.get("url"):
            host_port = pot["url"].split("//", 1)[-1].split("/", 1)[0]
            host, _, port = host_port.partition(":")
            checks.append({"check": "po-token-provider", "ok": tcp_reachable(host, int(port or 80)),
                           "detail": pot["url"]})
        usage = shutil.disk_usage(self.run_dir)
        checks.append({"check": "disk-space", "ok": usage.free > 2 * 1024**3,
                       "detail": f"{usage.free / 1024**3:.1f} GiB free"})
        checks.append({"check": "cgroup-v2", "ok": True, "detail": str(self.cgroup_base)})
        if not quick:
            checks.append(self._synthetic_accounting())
        result = {"schema_version": 1, "run_id": self.run_id, "kind": "preflight",
                  "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "checks": checks,
                  "ok": all(c["ok"] for c in checks)}
        write_json(self.run_dir / "preflight.json", result)
        return result

    def _synthetic_accounting(self) -> dict:
        """Parent + detached child allocation must show up in the scope total."""
        allocation = 64 * MIB
        code = (
            "import os, sys, time\n"
            f"size = {allocation}\n"
            "pid = os.fork()\n"
            "if pid:\n"
            "    os._exit(0)\n"
            "os.setsid()\n"
            "buf = bytearray(size)\n"
            "for i in range(0, size, 4096):\n"
            "    buf[i] = 1\n"
            "print('ready', flush=True)\n"
            "time.sleep(6)\n"
        )
        scope = Scope(self.cgroup_base, "preflight-synthetic")
        sampler = Sampler(scope, lambda: None, JsonlWriter(self.run_dir / "preflight-samples.jsonl"),
                          Registry(JsonlWriter(self.run_dir / "preflight-processes.jsonl"), self.run_id),
                          self.run_id, lambda: ["preflight"], sample_ms=100, pss_ms=1000,
                          session_name="preflight")
        detail = {}
        ok = False
        try:
            proc = subprocess.Popen([sys.executable, "-c", code],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            scope.move_pid(proc.pid)
            line = proc.stdout.readline().strip()
            detail["child_said"] = line
            time.sleep(0.4)
            sample = sampler.sample(kind="rss")
            total = sample["rss_bytes"]["total"]
            detail["observed_total_bytes"] = total
            detail["allocation_bytes"] = allocation
            detached = len(scope.procs()) >= 1
            detail["detached_child_in_scope"] = detached
            ok = total >= allocation * 0.7 and detached
            if proc.poll() is None:
                proc.kill()
        except Exception as exc:  # noqa: BLE001 - preflight reports any failure
            detail["error"] = repr(exc)
        finally:
            scope.kill_members(9)
            scope.await_empty(5.0)
            scope.remove(force=True)
        return {"check": "synthetic-child-accounting", "ok": ok, "detail": detail}

    # -- footprint ----------------------------------------------------------

    def measure_footprint(self) -> None:
        data: dict = {"schema_version": 1, "run_id": self.run_id, "kind": "footprint"}
        try:
            distribution = metadata.distribution("ytm")
        except metadata.PackageNotFoundError:
            distribution = None
        if distribution is not None:
            files = list(distribution.files or [])
            apparent = allocated = 0
            for file in files:
                try:
                    target = Path(distribution.locate_file(file))
                    stat = os.stat(target)
                except (OSError, TypeError):
                    continue
                apparent += stat.st_size
                allocated += stat.st_blocks * 512
            data["ytm_distribution"] = {
                "version": distribution.version, "root": str(distribution.locate_file("")),
                "files": len(files), "apparent_bytes": apparent, "allocated_bytes": allocated,
            }
            closure = {}
            todo = [distribution]
            seen = set()
            while todo:
                current = todo.pop()
                if current.metadata["Name"].lower() in seen:
                    continue
                seen.add(current.metadata["Name"].lower())
                closure[current.metadata["Name"]] = {
                    "version": current.version,
                    "direct": current.metadata["Name"].lower() == "ytm" or (
                        "ytm" in {r.split()[0].lower() for r in (distribution.requires or [])
                                  if r and not r.startswith(";")}),
                }
                for requirement in current.requires or []:
                    name = re.split(r"[\s<>=!~;\[]", requirement.strip())[0]
                    if not name:
                        continue
                    try:
                        todo.append(metadata.distribution(name))
                    except metadata.PackageNotFoundError:
                        closure[name] = {"version": None, "missing": True}
            data["python_dependencies"] = closure
        data["venv"] = dir_usage(REPO / ".venv")
        data["interpreter"] = {"executable": sys.executable, "version": sys.version.split()[0]}
        mpv_binary = shutil.which("mpv")
        if mpv_binary:
            package = run_capture(["pacman", "-Qi", "mpv"], timeout=10.0)
            installed = None
            if package:
                match = re.search(r"Installed Size\s*:\s*([\d.,]+)\s*([KMGT]?iB)",
                                  package)
                if match:
                    number = float(match.group(1).replace(",", "."))
                    scale = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3,
                             "TiB": 1024**4}.get(match.group(2), 1)
                    installed = number * scale
            data["mpv"] = {"binary": mpv_binary, "installed_bytes": installed,
                           "binary_bytes": os.stat(mpv_binary).st_size,
                           "package_manager": "pacman" if package else None}
            data["mpv_native"] = self._native_closure(mpv_binary)
        cache_dir = self.home / ".cache" / "ytm"
        state_dir = self.home / ".local" / "state" / "ytm"
        data["cache"] = {
            "before": self.cache_before,
            "after": dir_usage(cache_dir),
            "state_after": dir_usage(state_dir),
        }
        write_json(self.run_dir / "footprint.json", data)
        self.log("footprint: written")

    def _native_closure(self, binary: str) -> dict:
        ldd = run_capture(["ldd", binary], timeout=10.0)
        if not ldd:
            return {"available": False}
        libraries = sorted({line.split("=>")[1].strip().split(" ")[0]
                            for line in ldd.splitlines() if "=>" in line})
        packages: dict[str, int] = {}
        for library in libraries:
            owner = run_capture(["pacman", "-Qoq", library], timeout=5.0)
            if owner:
                package = owner.split()[0]
                if package not in packages:
                    info = run_capture(["pacman", "-Qi", package], timeout=5.0) or ""
                    match = re.search(r"Installed Size\s*:\s*([\d.,]+)\s*([KMGT]?iB)", info)
                    installed = 0.0
                    if match:
                        number = float(match.group(1).replace(",", "."))
                        scale = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3,
                                 "TiB": 1024**4}.get(match.group(2), 1)
                        installed = number * scale
                    packages[package] = installed
        return {"available": True, "libraries": len(libraries),
                "packages": packages,
                "installed_bytes": sum(packages.values())}

    # -- top level ----------------------------------------------------------

    def prepare(self) -> dict:
        staged = self.stage_profile()
        self.cgroup_base = find_delegated_base()
        self.cache_before = dir_usage(self.home / ".cache" / "ytm")
        self.manifest = collect_manifest(REPO, self.run_id, self.workload_hash, {
            "profile": getattr(self.args, "profile", "preflight"),
            "sample_ms": self.args.sample_ms,
            "pss_ms": self.args.pss_ms,
            "terminal": self.args.terminal_size,
            "term": "xterm-256color",
            "seed": self.args.seed,
            "dwell_seconds": self.args.dwell_seconds,
            "endurance_minutes": self.profile_values.get("endurance_minutes", 0),
            "longevity_tracks": self.profile_values.get("longevity_tracks"),
            "ytm_executable": self.executable,
            "authentication_staging": staged,
            "isolation": "private HOME + XDG dirs; real XDG_RUNTIME_DIR/audio bus",
        })
        self.manifest["cgroup"]["base"] = str(self.cgroup_base)
        write_json(self.run_dir / "manifest.json", self.manifest)
        self.event("run.started", monotonic_ns=now_ns(), fields={"run_id": self.run_id})
        return self.manifest

    def finish(self) -> dict:
        self.event("run.finished", monotonic_ns=now_ns())
        for writer in (self.events, self.samples, self.processes, self.trials):
            writer.close()
        summary = generate(self.run_dir)
        self.log(f"report: {self.run_dir / 'report.md'}")
        return summary


def _video_id(path):
    if not path:
        return None
    match = re.search(r"[?&]v=([\w-]{6,})", path)
    return match.group(1) if match else (Path(path).name or None)


def _sanitize_playback(result: dict) -> dict:
    clean = dict(result)
    clean["video_id"] = _video_id(clean.pop("path", None))
    return clean


def cmd_preflight(args) -> int:
    bench = Bench(args)
    bench.prepare()
    result = bench.preflight(quick=args.quick)
    for check in result["checks"]:
        print(f"{'PASS' if check['ok'] else 'FAIL'}  {check['check']}: {check['detail']}")
    print(f"preflight: {'OK' if result['ok'] else 'FAILED'} ({bench.run_dir})")
    bench.finish()
    return 0 if result["ok"] else 1


def cmd_run(args) -> int:
    profile = dict(PROFILES[args.profile])
    if args.startup_runs is not None:
        profile["startup_runs"] = args.startup_runs
    if args.search_runs is not None:
        profile["search_runs"] = args.search_runs
    if args.switches is not None:
        profile["switches"] = args.switches
    if args.endurance_minutes is not None:
        profile["endurance_minutes"] = args.endurance_minutes
    if args.longevity_tracks is not None:
        profile["longevity_tracks"] = args.longevity_tracks

    bench = Bench(args)
    bench.profile_values = profile
    bench.prepare()
    result = bench.preflight(quick=True)
    for check in result["checks"]:
        print(f"{'PASS' if check['ok'] else 'FAIL'}  {check['check']}: {check['detail']}")
    fatal = [c for c in result["checks"]
             if not c["ok"] and c["check"] in ("ytm-executable", "mpv", "audio-server", "cgroup-v2")]
    if fatal:
        print("preflight failed; refusing to measure", file=sys.stderr)
        bench.finish()
        return 2
    try:
        if profile["startup_runs"]:
            bench.log(f"startup trials: {profile['startup_runs']}")
            bench.run_startup_trials(profile["startup_runs"])
        if profile["search_runs"] or profile["switches"]:
            bench.log("interactive session")
            bench.run_interactive(profile)
        if profile["endurance_minutes"]:
            bench.log(f"endurance session: {profile['endurance_minutes']} min, "
                      f"{profile.get('longevity_tracks', 50)} tracks")
            bench.run_endurance(profile)
        bench.measure_footprint()
    except KeyboardInterrupt:
        bench.log("interrupted; writing partial artifacts")
        for scope in list(bench.scopes):
            scope.kill_members(9)
            scope.await_empty(5.0)
            scope.remove(force=True)
        bench.event("run.interrupted", monotonic_ns=now_ns())
    bench.finish()
    return 0


def cmd_footprint(args) -> int:
    bench = Bench(args)
    bench.cgroup_base = find_delegated_base()
    bench.cache_before = dir_usage(bench.home / ".cache" / "ytm")
    bench.manifest = collect_manifest(REPO, bench.run_id, bench.workload_hash,
                                      {"profile": "footprint"})
    write_json(bench.run_dir / "manifest.json", bench.manifest)
    bench.measure_footprint()
    bench.finish()
    return 0


def cmd_report(args) -> int:
    run_dir = Path(args.run_dir)
    summary = generate(run_dir)
    print(json.dumps({
        "run_id": summary["run_id"],
        "peak_rss_mib": (summary["peak"]["rss_bytes"]["total"] / MIB) if summary["peak"] else None,
        "report": str(run_dir / "report.md"),
    }, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmark.py", description=__doc__)
    parser.add_argument("--output", default=str(RESULTS_DIR),
                        help="artifact root (default: benchmarks/results)")
    parser.add_argument("--ytm-executable", default=None)
    parser.add_argument("--scenario-file", default=str(HERE / "scenarios.json"))
    parser.add_argument("--sample-ms", type=int, default=100)
    parser.add_argument("--pss-ms", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260925)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--terminal-size", default="40x120", help="ROWSxCOLS")
    parser.add_argument("--dwell-seconds", type=float, default=10.0,
                        help="playback time before a scripted switch")
    parser.add_argument("--inter-run-gap", type=float, default=1.0)
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight")
    preflight.add_argument("--quick", action="store_true")
    preflight.set_defaults(func=cmd_preflight)

    run = sub.add_parser("run")
    run.add_argument("--profile", choices=sorted(PROFILES), default="smoke")
    run.add_argument("--startup-runs", type=int, default=None)
    run.add_argument("--search-runs", type=int, default=None)
    run.add_argument("--switches", type=int, default=None)
    run.add_argument("--endurance-minutes", type=float, default=None)
    run.add_argument("--longevity-tracks", type=int, default=None)
    run.set_defaults(func=cmd_run)

    footprint = sub.add_parser("footprint")
    footprint.set_defaults(func=cmd_footprint)

    report = sub.add_parser("report")
    report.add_argument("run_dir")
    report.set_defaults(func=cmd_report)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # subcommand options may arrive before the subcommand; re-parse check
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
