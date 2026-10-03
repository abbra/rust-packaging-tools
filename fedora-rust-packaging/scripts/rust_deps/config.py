"""rust-deps shared constants and paths."""

from __future__ import annotations

from pathlib import Path
import os
import re

ROOT_ENV = "RUST_DEPS_ROOT"
CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "rust-packaging-tools"
)
SELF = (
    Path(__file__).resolve().parent.parent / "rust-deps"
)  # the launcher next to this package
USER_AGENT = (
    "rust-packaging-tools (Fedora packaging helper; https://src.fedoraproject.org)"
)
EDITS_FILE = "cargo-toml-edits.toml"
TARGETS_FILE = "targets.toml"  # only = ["fedora-44", ...]: build only for these targets
DIST_GIT_FILE = (
    "dist-git.toml"  # adopted from Fedora's dist-git: package, branch, commit, version
)
EDITOR_ENV = "RUST_DEPS_EDITS"

# Targets that never apply to Fedora builds.  Anything guarded by not(...) is
# kept; rust2rpm's automatic patch strips the rest of the foreign targets.
FOREIGN_RE = re.compile(
    r'wasm|windows|macos|"ios"|android|redox|wasi|emscripten|fuchsia|haiku|hermit|'
    r'solaris|illumos|freebsd|netbsd|openbsd|dragonfly|miri|"vxworks"|"uefi"'
)


LICENSE_RE = re.compile(r"^(LICEN[CS]E|COPYING|UNLICENSE|NOTICE)", re.IGNORECASE)
