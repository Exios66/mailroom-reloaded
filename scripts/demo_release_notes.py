#!/usr/bin/env python3
"""Print Markdown that embeds every demo screenshot, for PR bodies and GitHub release notes.

    python3 scripts/demo_release_notes.py --sha <commit> [--repo owner/name] [--private] [--manifest PATH]

Reads ``docs/demo/manifest.json`` (written by ``scripts/demo_capture.mjs``) and prints one inline
image per entry, then a captions list. Image URLs have the form

    https://github.com/<repo>/blob/<sha>/docs/demo/<file>?raw=true

which pins the image to a commit that contains it (push that commit first) and, unlike
raw.githubusercontent.com, also renders for a viewer who is signed in with access to a private
repository. ``--private`` adds a one-line note saying so. Stdlib only; never touches the network.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

DEFAULT_REPO = "Exios66/mailroom-reloaded"
DEFAULT_MANIFEST = Path(__file__).resolve().parent.parent / "docs" / "demo" / "manifest.json"
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/(?!\.{1,2}$)[A-Za-z0-9_.-]+$")
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.png$")


def load_manifest(path: Path) -> list[dict]:
    """Return the manifest's image entries, validating the fields this script relies on."""
    data = json.loads(path.read_text(encoding="utf-8"))
    images = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images, list) or not images:
        raise ValueError(f"{path}: no images in manifest")
    for entry in images:
        if not isinstance(entry, dict) or not _FILE_RE.match(str(entry.get("file", ""))):
            raise ValueError(f"{path}: bad image entry {entry!r}")
        caption = entry.get("caption")
        if not isinstance(caption, str) or not caption.strip():
            raise ValueError(f"{path}: {entry['file']} has no caption")
        if "<" in caption or "](" in caption or re.search(r"[\r\n]", caption):
            raise ValueError(f"{path}: {entry['file']} caption has '<', '](' or a line break")
    return images


def image_url(repo: str, sha: str, file: str) -> str:
    """Build the commit-pinned URL that also works for private repositories."""
    return f"https://github.com/{repo}/blob/{sha}/docs/demo/{quote(file)}?raw=true"


def _alt(caption: str) -> str:
    """Make a caption safe inside Markdown image alt text."""
    return " ".join(re.sub(r"[\[\]`]", "", caption).split())


def _text(caption: str) -> str:
    """Escape a caption for a Markdown list item: collapse whitespace, backslash-escape specials."""
    return re.sub(r"([\\`*_<>\[\]()!#])", r"\\\1", " ".join(caption.split()))


def render(images: list[dict], sha: str, repo: str = DEFAULT_REPO, private: bool = False) -> str:
    """Render the Markdown block: optional private note, inline images, then a captions list."""
    if not _SHA_RE.match(sha):
        raise ValueError(f"--sha must be 7-40 lowercase hex characters, got {sha!r}")
    if not _REPO_RE.match(repo):
        raise ValueError(f"--repo must look like owner/name, got {repo!r}")
    lines = ["## Demo screenshots", ""]
    if private:
        lines += [
            ("> Private repository: these images render only while you are signed in to GitHub "
             "with access to it."),
            "",
        ]
    lines += [f"Captured from `{sha}` (see `docs/demo/manifest.json`).", ""]
    for entry in images:
        lines += [f"![{_alt(entry['caption'])}]({image_url(repo, sha, entry['file'])})", ""]
    lines += ["### Captions", ""]
    lines += [f"- `{entry['file']}`: {_text(entry['caption'])}" for entry in images]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sha", required=True, help="commit that contains docs/demo/*.png")
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name (default {DEFAULT_REPO})")
    ap.add_argument("--private", action="store_true", help="add the signed-in-viewers note")
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = ap.parse_args(argv)
    try:
        sys.stdout.write(render(load_manifest(args.manifest), args.sha, args.repo, args.private))
    except (OSError, ValueError) as exc:
        print(f"demo_release_notes: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
