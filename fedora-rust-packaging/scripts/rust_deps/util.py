"""rust-deps output helpers: info/warn/action/die, visible width, subprocess run."""

from __future__ import annotations

import os
import re
import subprocess
import sys


def info(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


# OSC 8 hyperlinks when stdout is a terminal; otherwise print URLs in full, which
# terminals, logs and agent transcripts turn into links themselves
_HYPERLINKS = (
    sys.stdout.isatty()
    and os.environ.get("TERM") != "dumb"
    and not os.environ.get("RUST_DEPS_NO_HYPERLINKS")
)


def link(url: str, label: str | None = None, keep_label: bool = False) -> str:
    """A clickable reference to url: the label as a terminal hyperlink, or the URL
    itself (after the label with keep_label, when the URL does not show it)."""
    if label is not None and _HYPERLINKS:
        return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"
    return f"{label} {url}" if label is not None and keep_label else url


def visible_len(text: str) -> int:
    return len(re.sub(r"\033\]8;;[^\033]*\033\\", "", text))


def ljust_visible(text: str, width: int) -> str:
    return text + " " * max(0, width - visible_len(text))


def action(msg: str) -> None:
    """Something a human (or agent) must decide or fix."""
    print(f"ACTION NEEDED: {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, text=True, **kw)


# toolbox marks its containers with this file; inside one, running podman shares
# the host's container storage with an incompatible runroot and corrupts it
TOOLBOX_MARKER = "/run/.toolboxenv"


def in_toolbox() -> bool:
    """True inside a toolbox container, where podman must not run."""
    return os.path.exists(TOOLBOX_MARKER)
