"""scripts/demo_release_notes.py: URL shape, captions, validation, and the committed manifest."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("demo_release_notes", ROOT / "scripts" / "demo_release_notes.py")
drn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drn)

SHA = "502995aa6a98d0af62c02efe77ac7285bea0d15d"
IMAGES = [
    {"file": "01-tui-help.png", "caption": "The /tui terminal running `help`."},
    {"file": "02-replay-viewer.png", "caption": "Replay viewer [paused]."},
]


def _manifest(tmp_path: Path, images=IMAGES) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"source_commit": SHA, "images": images}), encoding="utf-8")
    return path


def test_every_image_is_embedded_with_the_pinned_blob_url():
    out = drn.render(IMAGES, SHA, "acme/widgets")
    for entry in IMAGES:
        url = f"https://github.com/acme/widgets/blob/{SHA}/docs/demo/{entry['file']}?raw=true"
        assert f"]({url})" in out
    assert out.count("![") == len(IMAGES)


def test_captions_list_follows_the_images():
    out = drn.render(IMAGES, SHA)
    head, _, tail = out.partition("### Captions")
    assert r"- `01-tui-help.png`: The /tui terminal running \`help\`." in tail
    assert r"- `02-replay-viewer.png`: Replay viewer \[paused\]." in tail
    assert "Exios66/mailroom-reloaded" in head  # default repo


def test_alt_text_cannot_break_the_markdown_image():
    out = drn.render(IMAGES, SHA)
    assert "![Replay viewer paused.](" in out
    assert "![The /tui terminal running help.](" in out


def test_alt_text_strips_backticks_and_collapses_whitespace():
    assert drn._alt("a `b` [c]  d") == "a b c d"
    assert drn._alt("end `x`.") == "end x."


def test_caption_markup_is_escaped_in_the_captions_list():
    images = [{"file": "a.png", "caption": "# Head *bold* _it_ !x (y) \\ `c`"}]
    tail = drn.render(images, SHA).partition("### Captions")[2]
    assert r"\# Head \*bold\* \_it\_ \!x \(y\) \\ \`c\`" in tail
    assert "\n# " not in tail


@pytest.mark.parametrize(
    "caption",
    ["<img src=x onerror=1>", "bad\nline", "bad\r", "![x](http://evil)", "see [a](b)", "<!-- c -->"],
)
def test_rejects_caption_injection(tmp_path, caption):
    with pytest.raises(ValueError):
        drn.load_manifest(_manifest(tmp_path, [{"file": "a.png", "caption": caption}]))


def test_rejects_non_string_caption(tmp_path):
    with pytest.raises(ValueError):
        drn.load_manifest(_manifest(tmp_path, [{"file": "a.png", "caption": ["x"]}]))


def test_private_note_only_with_flag():
    assert "Private repository" in drn.render(IMAGES, SHA, private=True)
    assert "Private repository" not in drn.render(IMAGES, SHA)


@pytest.mark.parametrize("sha", ["", "main", "XYZ1234", "abc", "g" * 40, "a" * 41])
def test_rejects_bad_sha(sha):
    with pytest.raises(ValueError):
        drn.render(IMAGES, sha)


@pytest.mark.parametrize("repo", ["", "owner", "a/b/c", "a b/c", "../x"])
def test_rejects_bad_repo(repo):
    with pytest.raises(ValueError):
        drn.render(IMAGES, SHA, repo)


@pytest.mark.parametrize(
    "images",
    [[], [{"file": "../x.png", "caption": "c"}], [{"file": "a.png", "caption": " "}], [{"file": "a.txt", "caption": "c"}]],
)
def test_rejects_bad_manifest(tmp_path, images):
    with pytest.raises(ValueError):
        drn.load_manifest(_manifest(tmp_path, images))


def test_main_prints_markdown_and_reports_errors(tmp_path, capsys):
    path = _manifest(tmp_path)
    assert drn.main(["--sha", SHA, "--repo", "o/r", "--manifest", str(path)]) == 0
    assert "https://github.com/o/r/blob/" in capsys.readouterr().out
    assert drn.main(["--sha", "nope", "--manifest", str(path)]) == 2
    assert "demo_release_notes:" in capsys.readouterr().err
    assert drn.main(["--sha", SHA, "--manifest", str(tmp_path / "missing.json")]) == 2


def test_committed_manifest_matches_the_images_on_disk():
    manifest = ROOT / "docs" / "demo" / "manifest.json"
    if not manifest.exists():
        pytest.skip("no committed demo manifest")
    for entry in drn.load_manifest(manifest):
        png = ROOT / "docs" / "demo" / entry["file"]
        data = png.read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], entry["file"]
        assert len(data) == entry["size_bytes"] < 400 * 1024, entry["file"]
