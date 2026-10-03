"""rust-deps crates.io access with a small on-disk cache."""

from __future__ import annotations

from pathlib import Path
import functools
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from . import versions

_last_request = 0.0


def _http_get(url: str) -> bytes:
    """GET with crates.io's crawler policy (at most one request per second)."""
    global _last_request
    delay = 1.0 - (time.monotonic() - _last_request)
    if delay > 0:
        time.sleep(delay)
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    finally:
        _last_request = time.monotonic()


def _cached_json(url: str, key: str, max_age: float | None) -> dict:
    path = config.CACHE_DIR / "cratesio" / key
    if path.exists() and (
        max_age is None or time.time() - path.stat().st_mtime < max_age
    ):
        return json.loads(path.read_text())
    data = json.loads(_http_get(url))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


@functools.cache
def crate_versions(name: str) -> list[dict]:
    """Non-yanked releases of a crate, newest first."""
    try:
        data = _cached_json(
            f"https://crates.io/api/v1/crates/{name}", f"{name}.json", 86400
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []  # no such crate: callers say "not found on crates.io"
        raise
    return [v for v in data["versions"] if not v["yanked"]]


def forget_crate_versions(names) -> None:
    """Drop the cached release lists: a release published today is not in them."""
    for n in names:
        (config.CACHE_DIR / "cratesio" / f"{n}.json").unlink(missing_ok=True)
    crate_versions.cache_clear()


@functools.cache
def crate_dependencies(name: str, version: str) -> list[dict]:
    data = _cached_json(
        f"https://crates.io/api/v1/crates/{name}/{version}/dependencies",
        f"{name}-{version}-deps.json",
        None,  # published versions are immutable
    )
    return data["dependencies"]


def pick_version(name: str, req: str = "*") -> dict | None:
    """Newest stable crates.io release matching req."""
    for v in crate_versions(name):
        if "-" not in v["num"] and versions.req_matches(req, v["num"]):
            return v
    for v in crate_versions(name):  # fall back to pre-releases
        if versions.req_matches(req, v["num"]):
            return v
    return None


def download_crate(name: str, version: str, dest: Path) -> Path:
    path = dest / f"{name}-{version}.crate"
    if not path.exists():
        data = _http_get(f"https://crates.io/api/v1/crates/{name}/{version}/download")
        path.write_bytes(data)
    return path
