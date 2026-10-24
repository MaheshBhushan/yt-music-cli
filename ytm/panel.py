"""Loopback HTTP adapter for the local React control panel.

Run from a source checkout with ``python -m ytm.panel`` after building
``control-panel``. The existing Backend owns this session's mpv process.
"""

import argparse
import json
import math
import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ytm.tui.backend import Backend, BackendError

NO_ARGS = {"toggle", "pause", "resume", "next", "prev", "queue_clear"}
TRACK_COMMANDS = {"play", "enqueue", "enqueue_next"}
QUEUE_COMMANDS = {"queue_remove", "queue_move", "queue_play"}
TRACK_TEXT = {"video_id", "title", "artist", "album", "duration", "thumbnail"}
MAX_BODY = 16_384


def validate_command(payload):
    """Accept only the small, typed request vocabulary used by the panel."""
    if not isinstance(payload, dict) or set(payload) != {"cmd", "args"}:
        raise TypeError("Expected cmd and args")
    cmd, args = payload["cmd"], payload["args"]
    if not isinstance(cmd, str) or not isinstance(args, dict):
        raise TypeError("Expected a command string and an args object")
    if cmd in NO_ARGS:
        valid = not args
    elif cmd == "search":
        query = args.get("query")
        valid = (set(args) == {"query"} and isinstance(query, str)
                 and 0 < len(query.strip()) <= 200)
    elif cmd in TRACK_COMMANDS:
        valid = (not (set(args) - TRACK_TEXT - {"duration_seconds"})
                 and all(isinstance(args[k], str) and len(args[k]) <= 2000
                         for k in args.keys() & TRACK_TEXT)
                 and isinstance(args.get("video_id"), str)
                 and re.fullmatch(r"[A-Za-z0-9_-]{11}", args["video_id"]) is not None
                 and type(args.get("duration_seconds", 0)) is int
                 and 0 <= args.get("duration_seconds", 0) <= 604800)
    elif cmd in QUEUE_COMMANDS:
        keys = {"from_index", "to_index"} if cmd == "queue_move" else {"index"}
        valid = set(args) == keys and all(type(v) is int and v >= 0 for v in args.values())
    elif cmd in {"seek", "volume"}:
        key = "seconds" if cmd == "seek" else "level"
        value = args.get(key)
        valid = (set(args) == {key} and type(value) in (int, float)
                 and (-3600 <= value <= 3600 if cmd == "seek" else 0 <= value <= 100)
                 and math.isfinite(value))
    else:
        valid = False
    if not valid:
        raise ValueError("Unsupported command or invalid arguments")
    return cmd, args


def create_server(backend, assets, port=8765):
    """Bind only IPv4 loopback. No CORS, remote origins, or remote hosts."""
    assets = Path(assets).resolve()
    player_lock = threading.RLock()
    search_lock = threading.Lock()

    class Handler(SimpleHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format, *args):
            # Search queries and track metadata do not belong in HTTP logs.
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
            super().end_headers()

        def _json(self, status, data):
            body = json.dumps(data, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _local(self):
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            host = self.headers.get("Host")
            origin = self.headers.get("Origin")
            if host not in hosts or (origin is not None and origin != f"http://{host}"):
                self._json(403, {"error": "Only the local panel origin is allowed"})
                return False
            return True

        def do_GET(self):
            if not self._local():
                return
            if self.path == "/api/status":
                try:
                    with player_lock:
                        data = {"status": backend.request("status"), "queue": backend.request("queue_get")}
                    self._json(200, data)
                except BackendError as exc:
                    self._json(502, {"error": str(exc)})
            elif self.path.startswith("/api/"):
                self._json(404, {"error": "Unknown endpoint"})
            else:
                super().do_GET()

        def do_HEAD(self):
            if self._local():
                super().do_HEAD()

        def list_directory(self, path):
            self.send_error(404)

        def translate_path(self, path):
            resolved = Path(super().translate_path(path)).resolve()
            # Static assets must stay inside dist, including symlink targets.
            return str(resolved if resolved.is_relative_to(assets) else assets / ".missing")

        def do_POST(self):
            if not self._local():
                return
            if self.path != "/api/command":
                self._json(404, {"error": "Unknown endpoint"})
                return
            if self.headers.get_content_type() != "application/json":
                self._json(415, {"error": "Use application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Invalid request size")
                cmd, args = validate_command(json.loads(self.rfile.read(length)))
                # Search can take seconds. Keep transport controls responsive.
                with search_lock if cmd == "search" else player_lock:
                    if cmd in QUEUE_COMMANDS:
                        count = len(backend.request("queue_get")["tracks"])
                        if any(index >= count for index in args.values()):
                            raise ValueError("Queue entry is no longer available; refresh the queue")
                    result = backend.request(cmd, args)
                self._json(200, result)
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except BackendError as exc:
                self._json(502, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(assets)))
    server.daemon_threads = False
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--assets", type=Path, default=Path("control-panel/dist"))
    args = parser.parse_args(argv)
    if not (args.assets / "index.html").is_file():
        parser.error("Build the panel first: npm --prefix control-panel ci && npm --prefix control-panel run build")
    backend = None
    try:
        backend = Backend()
        with create_server(backend, args.assets, args.port) as server:
            print(f"ytm panel: http://127.0.0.1:{server.server_port} (Ctrl+C stops this player)", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (BackendError, OSError) as exc:
        parser.exit(1, f"ytm panel: {exc}\n")
    finally:
        if backend is not None:
            backend.close()


if __name__ == "__main__":
    main()
