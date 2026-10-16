#!/usr/bin/env python3
"""Build Pages efficiently when a push changes only its HTML template."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

import sync


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ONLY_PATHS = {"scripts/page.html", "site/index.html"}
SCRIPT_PATTERN = re.compile(
    r'<script id="collection-data" type="application/json">(.*?)</script>', re.DOTALL
)
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def git(*args, check=False):
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=check
    )


def usable_sha(value):
    return bool(value and SHA_PATTERN.fullmatch(value) and set(value) != {"0"})


def have_commit(sha):
    return git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def resolve_base(sha):
    """Return a usable comparison commit, fetching only a valid missing SHA."""
    if not usable_sha(sha):
        return None
    if have_commit(sha):
        return sha
    fetched = git("fetch", "--no-tags", "--depth=1", "--filter=blob:none", "origin", sha)
    return sha if fetched.returncode == 0 and have_commit(sha) else None


def changed_paths(base):
    result = git("diff", "--no-renames", "--name-only", "-z", base, "--")
    if result.returncode:
        return None
    return {path for path in result.stdout.split("\0") if path}


def collection_payload():
    index = ROOT / "site/index.html"
    match = SCRIPT_PATTERN.search(index.read_text()) if index.exists() else None
    if not match:
        return None
    encoded = match.group(1)
    try:
        payload = json.loads(encoded)
        songs = payload["songs"]
        count = payload["count"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    if not isinstance(songs, list) or type(count) is not int or count != len(songs):
        return None
    ids = [song.get("recording_mbid") for song in songs if isinstance(song, dict)]
    if any(not isinstance(recording_id, str) or not recording_id for recording_id in ids):
        return None
    if len(ids) != len(songs) or len(set(ids)) != len(ids):
        return None
    return encoded


def build_template_only():
    encoded = collection_payload()
    if encoded is None:
        return False
    template = (ROOT / "scripts/page.html").read_text()
    if "<!-- COLLECTION_DATA -->" not in template:
        return False
    sync.atomic_write(ROOT / "site/index.html", template.replace("<!-- COLLECTION_DATA -->", encoded))
    sync.atomic_write(ROOT / "site/.nojekyll", "")
    return True


def build(base):
    paths = changed_paths(base) if base else None
    if paths and paths <= TEMPLATE_ONLY_PATHS and build_template_only():
        return "template-only"
    sync.build_exports()
    return "full"


def site_changed(base):
    """A missing base is unsafe to skip: publish conservatively."""
    if not base:
        return True
    if git("diff", "--quiet", base, "--", "site").returncode != 0:
        return True
    untracked = git("ls-files", "--others", "--exclude-standard", "site")
    return untracked.returncode != 0 or bool(untracked.stdout.strip())


def write_output(name, value):
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as handle:
            handle.write(f"{name}={value}\n")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args(argv)
    is_push = os.environ.get("GITHUB_EVENT_NAME") == "push"
    base_sha = os.environ.get("GITHUB_EVENT_BEFORE") if is_push else os.environ.get("GITHUB_SHA")
    base = resolve_base(base_sha)
    if args.build:
        mode = build(base)
        print(f"Pages build: {mode}")
    publish = site_changed(base)
    write_output("publish", str(publish).lower())
    print(f"publish={str(publish).lower()}")


if __name__ == "__main__":
    main()
