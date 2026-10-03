"""rust-deps Fedora crate index: what is already packaged in a chroot."""

from __future__ import annotations

from dataclasses import dataclass, field
import collections
import os
import re
import time

from . import config
from . import util
from . import versions


@dataclass
class FedoraIndex:
    # crate name -> version -> set of features ("" = the crate itself)
    crates: dict[str, dict[str, set[str]]]

    def versions(self, name: str) -> list[str]:
        return sorted(
            self.crates.get(name, {}),
            key=lambda v: versions.parse_version(v) or versions.Version.parse("0.0.0"),
        )

    def best(self, name: str, req: str) -> str | None:
        matching = [v for v in self.versions(name) if versions.req_matches(req, v)]
        return matching[-1] if matching else None

    def missing_features(
        self, name: str, version: str, features: list[str]
    ) -> list[str]:
        have = self.crates.get(name, {}).get(version, set())
        return [f for f in features if f not in have]


def chroot_metalinks(chroot: str) -> list[str]:
    """Fedora mirror metalink repos that hold the crates of a COPR/mock chroot.

    Only the repositories that ship rust-*-devel packages: Fedora, or EPEL for
    the RHEL/CentOS based chroots (RHEL and CentOS ship no crates).  COPR's
    rhel+epel-N (N >= 10) uses EPEL for the current RHEL minor ("epel-z").
    """
    m = re.fullmatch(r"(.+)-(\d+|rawhide)-([^-]+)", chroot)
    if not m:
        util.die(
            f"cannot parse chroot {chroot!r} (expected e.g. fedora-44-x86_64, rhel+epel-10-x86_64)"
        )
    dist, rel = m.group(1), m.group(2)
    if dist == "fedora":
        return (
            ["rawhide"]
            if rel == "rawhide"
            else [f"fedora-{rel}", f"updates-released-f{rel}"]
        )
    if dist == "rhel+epel":
        return [f"epel-z-{rel}" if int(rel) >= 10 else f"epel-{rel}"]
    if dist in ("epel", "centos-stream+epel", "almalinux+epel", "rocky+epel"):
        return [f"epel-{rel}"]
    if dist == "centos-stream+epel-next":
        return [f"epel-{rel}", f"epel-next-{rel}"]
    util.die(f"do not know the crate repositories of chroot {chroot}")


def chroot_release(chroot: str) -> str:
    """The chroot without its architecture: crates are noarch, so all arches share one index."""
    return chroot.rsplit("-", 1)[0]


def fedora_index(refresh: bool = False, chroot: str | None = None) -> FedoraIndex:
    """All crate(...) provides of rust-*-devel packages in the enabled repos.

    Cached for a day; set RUST_DEPS_DNF_ARGS (e.g. "--releasever=rawhide") to
    query a different release than the running system, or pass a COPR/mock
    chroot (e.g. "rhel+epel-10-x86_64") to query that target's repositories.
    """
    if chroot:
        tag = "chroot-" + re.sub(r"[^A-Za-z0-9_.=-]", "_", chroot_release(chroot))
        repodir = config.CACHE_DIR / "repos" / tag
        repodir.mkdir(parents=True, exist_ok=True)
        (repodir / "crates.repo").write_text(
            "".join(
                f"[{r}]\nname={r}\ngpgcheck=0\n"
                f"metalink=https://mirrors.fedoraproject.org/metalink?repo={r}&arch=x86_64\n\n"
                for r in chroot_metalinks(chroot)
            )
        )
        dnfcache = config.CACHE_DIR / "dnf"
        extra = [
            f"--setopt=reposdir={repodir}",
            f"--setopt=cachedir={dnfcache}",
            f"--setopt=system_cachedir={dnfcache}",
        ]
        where = f"{chroot_release(chroot)} repositories ({', '.join(chroot_metalinks(chroot))})"
    else:
        extra = os.environ.get("RUST_DEPS_DNF_ARGS", "").split()
        tag = re.sub(r"[^A-Za-z0-9_.=-]", "_", "_".join(extra)) or "system"
        where = "Fedora repositories"
    cache = config.CACHE_DIR / f"fedora-crates-{tag}.txt"
    if refresh or not cache.exists() or time.time() - cache.stat().st_mtime > 86400:
        util.info(f"Querying {where} for packaged crates (cached for 24h)…")
        res = util.run(
            ["dnf", "-q", "repoquery", *extra, "--provides", "rust-*-devel"],
            capture_output=True,
        )
        if res.returncode != 0:
            util.die(f"dnf repoquery failed:\n{res.stderr}")
        lines = sorted(
            {
                ln.strip()
                for ln in res.stdout.splitlines()
                if ln.startswith("crate(") and " = " in ln
            }
        )
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("\n".join(lines) + "\n")
    crates: dict[str, dict[str, set[str]]] = collections.defaultdict(
        lambda: collections.defaultdict(set)
    )
    for ln in cache.read_text().splitlines():
        m = re.match(r"crate\(([^/)]+)(?:/([^)]*))?\) = (\S+)", ln)
        if m:
            name, feat, ver = m.groups()
            crates[name][ver].add(feat or "")
    return FedoraIndex(crates)
