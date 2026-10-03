"""rust-deps version parsing and requirement matching (cargo2rpm semver)."""

from __future__ import annotations

import re
import sys

from . import config

try:
    from cargo2rpm.semver import Version, VersionReq
except ImportError:  # pragma: no cover
    sys.exit("rust-deps: python3-cargo2rpm is required (dnf install rust2rpm)")


def parse_version(v: str) -> Version | None:
    try:
        return Version.parse(v)
    except Exception:
        return None


def req_matches(req: str, version: str) -> bool:
    v = parse_version(version)
    if v is None:
        return False
    try:
        return v in VersionReq.parse(req)
    except Exception:
        return False


def is_foreign(target: str | None) -> bool:
    if not target:
        return False
    if not target.startswith("cfg("):
        return "linux" not in target
    if "not(" in target or re.search(r"\bunix\b|linux", target):
        return False
    return bool(config.FOREIGN_RE.search(target))
