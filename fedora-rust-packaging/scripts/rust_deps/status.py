"""rust-deps status: the state of the local tree."""

from __future__ import annotations

from pathlib import Path
import contextlib
import os
import re

from . import config
from . import cratesio
from . import fedora
from . import packages
from . import targets
from . import util


def host_release(os_release: Path = Path("/etc/os-release")) -> str:
    """The release the host's repositories are, e.g. fedora-45, as a status column heading."""
    osr = {}
    with contextlib.suppress(OSError):
        for ln in os_release.read_text().splitlines():
            key, _, value = ln.partition("=")
            osr[key] = value.strip('"')
    name = (
        f"{osr.get('ID', 'host')}-{osr['VERSION_ID']}"
        if osr.get("VERSION_ID")
        else osr.get("ID", "host")
    )
    extra = os.environ.get("RUST_DEPS_DNF_ARGS")
    return f"{name} ({extra})" if extra else name


def release_chroot(target: str) -> str:
    """A chroot for a release or chroot name: 'fedora-44' -> 'fedora-44-x86_64' (crates are noarch)."""
    return target if re.search(r"-(\d+|rawhide)-[^-]+$", target) else f"{target}-x86_64"


def version_cell(
    versions: list[str], local: str | None, all_versions: bool = False
) -> str:
    """The versions a release has: all of them, or the newest (=: the packaged one) and how many more."""
    if not versions:
        return "-"
    if all_versions:
        return ",".join(v + ("=" if v == local else "") for v in versions)
    newest = versions[-1]
    more = len(versions) - 1
    return newest + ("=" if newest == local else "") + (f" (+{more})" if more else "")


def cmd_status(args) -> None:
    chroots = list(args.target) + (
        targets.project_chroots(args.project) if args.project else []
    )
    releases: dict[str, fedora.FedoraIndex] = {}
    if not chroots:
        releases[host_release()] = fedora.fedora_index(args.refresh)
    for t in chroots:
        chroot = release_chroot(t)
        releases.setdefault(
            fedora.chroot_release(chroot), fedora.fedora_index(args.refresh, chroot)
        )
    pkgs = packages.select(args.root, args.crates, args.all or not args.crates)
    if args.refresh:
        cratesio.forget_crate_versions(p.crate for p in pkgs)
    show_targets = any(p.limited for p in pkgs)
    rows = [
        (
            "crate",
            "packaged",
            "crates.io",
            *releases,
            *(["for"] if show_targets else []),
            "patch",
            "srpm",
        )
    ]
    for pkg in pkgs:
        latest = cratesio.pick_version(pkg.crate)
        srpm = list(pkg.dir.glob(f"{pkg.rpm_name}-{pkg.version}-*.src.rpm"))
        rows.append(
            (
                pkg.crate + (" (compat)" if pkg.compat else ""),
                pkg.version or "-",
                (latest or {}).get("num", "?")
                + ("" if latest and latest["num"] == pkg.version else " *"),
                *(
                    version_cell(
                        idx.versions(pkg.crate), pkg.version, args.all_versions
                    )
                    for idx in releases.values()
                ),
                *(
                    [
                        (
                            (
                                "where missing"
                                if pkg.dist_git is not None
                                else ",".join(pkg.targets)
                            )
                            if pkg.limited
                            else "all"
                        )
                    ]
                    if show_targets
                    else []
                ),
                "yes" if (pkg.dir / f"{pkg.crate}-fix-metadata.diff").exists() else "-",
                "yes" if srpm else "-",
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        util.info("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    util.info(
        "\n* = newer version on crates.io ('rust-deps update <crate>' to update; --refresh "
        "for a release of the last day)"
    )
    util.info(
        "= : the version packaged here; (+N): N older versions as well"
        + ("" if args.all_versions else " (--all-versions lists them)")
    )
    if show_targets:
        util.info(
            f"for: the targets a package is built for ({config.TARGETS_FILE}); 'where missing': adopted from "
            f"Fedora ({config.DIST_GIT_FILE}), built where a target lacks this version"
        )
