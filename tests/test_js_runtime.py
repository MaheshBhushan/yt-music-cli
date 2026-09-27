"""D7: one JavaScript-runtime decision for playback and downloads.

Playback (mpv's yt-dlp raw option) and the Python download path must agree
on which runtime may solve YouTube challenges; the audit found playback
selecting Node while downloads kept yt-dlp's Deno-only default.
"""

from ytm import cache, js_runtime
from ytm.player import mpv_args


def which(*available):
    def fake(name):
        return f"/usr/bin/{name}" if name in available else None

    return fake


def test_deno_is_still_preferred_when_both_are_installed(monkeypatch):
    monkeypatch.setattr(js_runtime.shutil, "which", which("deno", "node"))
    assert js_runtime.find() == "deno"
    assert js_runtime.ytdlp_option() == {"deno": {}}


def test_node_only_is_used_for_download_and_playback(monkeypatch):
    monkeypatch.setattr(js_runtime.shutil, "which", which("node"))
    assert js_runtime.find() == "node"
    assert js_runtime.ytdlp_option() == {"node": {}}
    assert cache._ydl_opts("out.%(ext)s")["js_runtimes"] == {"node": {}}
    args = mpv_args("/tmp/mpv.sock", js_runtimes=js_runtime.find())
    assert "--ytdl-raw-options=js-runtimes=node" in args


def test_deno_only_is_used_for_download_and_playback(monkeypatch):
    monkeypatch.setattr(js_runtime.shutil, "which", which("deno"))
    assert js_runtime.find() == "deno"
    assert cache._ydl_opts("out.%(ext)s")["js_runtimes"] == {"deno": {}}
    args = mpv_args("/tmp/mpv.sock", js_runtimes=js_runtime.find())
    assert "--ytdl-raw-options=js-runtimes=deno" in args


def test_neither_runtime_keeps_the_documented_fallback(monkeypatch):
    monkeypatch.setattr(js_runtime.shutil, "which", lambda name: None)
    assert js_runtime.find() is None
    assert js_runtime.ytdlp_option() is None
    assert "js_runtimes" not in cache._ydl_opts("out.%(ext)s")
    args = mpv_args("/tmp/mpv.sock", js_runtimes=js_runtime.find())
    assert not any("js-runtimes" in arg for arg in args)


def test_ytdlp_normalizes_the_selected_runtime(monkeypatch):
    """The mapping actually reaches yt-dlp's normalized parameters."""
    monkeypatch.setattr(js_runtime.shutil, "which", which("node"))
    from yt_dlp import YoutubeDL

    with YoutubeDL(cache._ydl_opts("out.%(ext)s")) as ydl:
        assert set(ydl.params["js_runtimes"]) == {"node"}


def test_a_runtime_path_with_spaces_is_never_embedded(monkeypatch):
    """Only the runtime name travels: mpv's raw option list is comma-joined,
    so embedding an executable path is the fragile choice."""
    monkeypatch.setattr(
        js_runtime.shutil, "which",
        lambda name: f"/opt/My Tools/{name}" if name == "node" else None,
    )
    assert js_runtime.find() == "node"
    assert js_runtime.ytdlp_option() == {"node": {}}
    args = mpv_args("/tmp/mpv.sock", js_runtimes=js_runtime.find())
    assert "--ytdl-raw-options=js-runtimes=node" in args
    assert not any("My Tools" in arg for arg in args)


def test_runtime_selection_does_not_change_download_policy(monkeypatch):
    monkeypatch.setattr(js_runtime.shutil, "which", which("node"))
    opts = cache._ydl_opts("out.%(ext)s")
    assert opts["format"] == "bestaudio"
    assert "cookies" not in opts
    assert "username" not in opts
