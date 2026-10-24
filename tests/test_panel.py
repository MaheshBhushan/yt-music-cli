"""Exercise the real HTTP adapter and Backend with deterministic mpv fixtures."""

import http.client
import json
import threading

import pytest

from tests.panel_fixture import PanelPlayer, search
from ytm import music, state
from ytm.panel import MAX_BODY, create_server
from ytm.tui.backend import Backend


@pytest.fixture
def panel(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(music, "search", search)
    assets = tmp_path / "dist"
    assets.mkdir()
    (assets / "index.html").write_text("<h1>Local player</h1>")
    (assets / "app.js").write_text("console.log('panel')")
    player = PanelPlayer()
    backend = Backend(player_factory=lambda **kwargs: player)
    server = create_server(backend, assets, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method="GET", path="/api/status", payload=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        body = raw if raw is not None else json.dumps(payload) if payload is not None else None
        connection.request(method, path, body=body, headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    yield request, player, server, tmp_path
    server.shutdown()
    server.server_close()
    thread.join(2)
    backend.close()


def test_search_queue_and_transport_through_http(panel):
    request, player, _, _ = panel

    def cmd(name, args=None):
        code, body, _ = request("POST", "/api/command", {"cmd": name, "args": args or {}})
        assert code == 200, body
        return json.loads(body)

    tracks = cmd("search", {"query": "satellites"})["tracks"]
    assert [t["title"] for t in tracks] == ["Blue Hour", "Night Drive", "Morning Tide"]
    cmd("play", tracks[0])
    cmd("enqueue", tracks[1])
    cmd("enqueue_next", tracks[2])
    cmd("queue_move", {"from_index": 2, "to_index": 1})
    cmd("toggle")
    cmd("seek", {"seconds": 10})
    cmd("volume", {"level": 35})
    code, body, _ = request()
    data = json.loads(body)
    assert code == 200
    assert [t["title"] for t in data["queue"]["tracks"]] == ["Blue Hour", "Night Drive", "Morning Tide"]
    assert data["status"]["current"]["title"] == "Blue Hour"
    assert data["status"]["paused"] is True
    assert data["status"]["position"] == 10
    assert data["status"]["volume"] == 35
    cmd("resume")
    cmd("next")
    assert player.status()["index"] == 1
    cmd("prev")
    assert player.status()["index"] == 0
    cmd("queue_play", {"index": 2})
    cmd("queue_remove", {"index": 0})
    cmd("queue_clear")
    data = json.loads(request()[1])
    assert [t["title"] for t in data["queue"]["tracks"]] == ["Morning Tide"]
    assert data["queue"]["index"] == 0


@pytest.mark.parametrize("payload", [
    None, [], {"cmd": [], "args": {}}, {"cmd": "shutdown", "args": {}},
    {"cmd": "search", "args": {"query": " "}},
    {"cmd": "search", "args": {"query": 5}},
    {"cmd": "play", "args": {"video_id": "file:///etc/passwd"}},
    {"cmd": "enqueue", "args": {"video_id": "fixture0001", "duration_seconds": -1}},
    {"cmd": "volume", "args": {"level": 101}},
    {"cmd": "volume", "args": {"level": True}},
    {"cmd": "seek", "args": {"seconds": float("nan")}},
    {"cmd": "seek", "args": {"seconds": 10**400}},
    {"cmd": "queue_remove", "args": {"index": -1}},
    {"cmd": "queue_play", "args": {"index": 0}},
    {"cmd": "queue_move", "args": {"from_index": 0}},
    {"cmd": "pause", "args": {"unexpected": 1}},
])
def test_reject_invalid_requests_without_player_changes(panel, payload):
    request, player, _, _ = panel
    code, body, _ = request("POST", "/api/command", raw=json.dumps(payload))
    assert code == 400
    assert "error" in json.loads(body)
    assert player.calls == []


def test_local_origin_and_http_boundaries(panel):
    request, player, server, _ = panel
    assert server.server_address[0] == "127.0.0.1"
    for headers in [{"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Origin": "null"}]:
        assert request(headers=headers)[0] == 403
        assert request("POST", "/api/command", {"cmd": "pause", "args": {}}, headers)[0] == 403
    assert request("POST", "/api/command", raw="{")[0] == 400
    assert request("POST", "/api/command", raw="x" * (MAX_BODY + 1))[0] == 400
    assert request("POST", "/api/command", raw="{}", headers={"Content-Type": "text/plain"})[0] == 415
    assert request("POST", "/api/unknown", {})[0] == 404
    assert request(path="/api/unknown")[0] == 404
    assert player.calls == []


def test_static_assets_stay_inside_build_directory(panel):
    request, _, _, tmp_path = panel
    (tmp_path / "secret.txt").write_text("private")
    (tmp_path / "dist" / "escape.txt").symlink_to(tmp_path / "secret.txt")
    code, body, headers = request(path="/")
    assert code == 200 and b"Local player" in body
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert request(path="/app.js")[0] == 200
    assert request(path="/escape.txt")[0] == 404
    assert request(path="/../secret.txt")[0] == 404


def test_empty_results_and_backend_errors_are_json(panel):
    request, _, _, _ = panel
    def search_for(query):
        code, body, _ = request("POST", "/api/command", {"cmd": "search", "args": {"query": query}})
        return code, json.loads(body)
    assert search_for("empty") == (200, {"tracks": []})
    assert search_for("offline") == (502, {"error": "Fixture catalogue is offline"})
