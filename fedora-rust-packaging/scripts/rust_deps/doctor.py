"""rust-deps doctor: check the prerequisites."""

from __future__ import annotations

import shutil

from . import config
from . import packages
from . import trial
from . import util

REQUIRED_TOOLS = {
    "rust2rpm": "rust2rpm",
    "cargo": "cargo",
    "rpmbuild": "rpm-build",
    "spectool": "rpmdevtools",
    "rpmlint": "rpmlint",
    "dnf": "dnf5",
    "patch": "patch",
    "unshare": "util-linux",
    "ip": "iproute",
}
OPTIONAL_TOOLS = {
    "mock": "mock (for mock-chain)",
    "copr-cli": "copr-cli (for copr)",
    "fedpkg": "fedpkg (for dist-git)",
    "fedora-review": "fedora-review (for review)",
}


def cmd_doctor(args) -> None:
    ok = True
    for tool, pkg in REQUIRED_TOOLS.items():
        found = shutil.which(tool)
        ok &= bool(found)
        util.info(
            f"   {'ok     ' if found else 'MISSING'} {tool:10} {found or f'dnf install {pkg}'}"
        )
    for tool, pkg in OPTIONAL_TOOLS.items():
        found = shutil.which(tool)
        util.info(f"   {'ok     ' if found else 'absent '} {tool:10} {found or pkg}")
    if shutil.which("mock"):
        in_group = "mock" in util.run(["id", "-nG"], capture_output=True).stdout.split()
        util.info(
            f"   {'ok     ' if in_group else 'WARNING'} mock group {'' if in_group else '(sudo usermod -aG mock $USER, then log in again)'}"
        )
    res = (
        util.run([*trial.OFFLINE_PREFIX, "true"], capture_output=True)
        if shutil.which("unshare") and shutil.which("ip")
        else None
    )
    if res is not None and res.returncode:
        ok = False
        util.info(
            "   MISSING user namespaces with loopback: offline trials need 'unshare -rn' and "
            f"'ip link set lo up' in it (or use 'trial --online'): {res.stderr.strip()}"
        )
    if args.root.is_dir():
        pkgs = packages.local_packages(args.root)
        util.info(f"   root    {args.root} ({len(pkgs)} packages)")
    else:
        ok = False
        util.info(
            f"   MISSING root {args.root} is not a directory (wrong --root or {config.ROOT_ENV}?)"
        )
    if not ok:
        util.die("install the missing tools listed above")
