"""rust-deps local packages: the <root>/<crate> tree and its selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib

from . import config
from . import copr
from . import fedora
from . import util


@dataclass
class LocalPackage:
    crate: str
    dir: Path
    version: str | None  # None until a spec has been generated
    name: str | None = (
        None  # RPM name, from the spec's file name; None until there is one
    )

    @property
    def rpm_name(self) -> str:
        return self.name or f"rust-{self.crate}"

    @property
    def compat(self) -> bool:
        """A compat package (rust2rpm --compat): rust-<crate><major.minor>, installable next to
        other versions of the crate."""
        return self.rpm_name != f"rust-{self.crate}"

    @property
    def spec(self) -> Path:
        return self.dir / f"{self.rpm_name}.spec"

    @property
    def crate_file(self) -> Path | None:
        return self.dir / f"{self.crate}-{self.version}.crate" if self.version else None

    @property
    def edits_file(self) -> Path:
        return self.dir / config.EDITS_FILE

    @property
    def config_file(self) -> Path:
        return self.dir / "rust2rpm.toml"

    @property
    def targets(self) -> list[str] | None:
        """Target releases (e.g. "fedora-44", "rhel+epel-10") this package is
        limited to, from targets.toml; None means every chroot."""
        f = self.dir / config.TARGETS_FILE
        return tomllib.loads(f.read_text()).get("only") if f.exists() else None

    @property
    def dist_git(self) -> dict | None:
        """Where the packaging was adopted from (dist-git.toml), for a package that is in Fedora."""
        f = self.dir / config.DIST_GIT_FILE
        return tomllib.loads(f.read_text()) if f.exists() else None

    @property
    def limited(self) -> bool:
        """Not built everywhere, and not for a Fedora review: targets.toml, or adopted from dist-git."""
        return self.targets is not None or self.dist_git is not None

    @property
    def scope(self) -> str:
        if self.dist_git is not None:
            return f"in Fedora; built where a target lacks {self.version} ({config.DIST_GIT_FILE})"
        if self.targets is not None:
            return f"only for {', '.join(self.targets)} ({config.TARGETS_FILE})"
        return "everywhere"

    def builds_in(self, chroot: str) -> bool:
        if (
            self.dist_git is not None
        ):  # where the target does not have this version (yet)
            return self.version not in copr.target_index(chroot).versions(self.crate)
        t = self.targets
        return t is None or chroot in t or fedora.chroot_release(chroot) in t


def local_packages(root: Path) -> dict[str, LocalPackage]:
    pkgs = {}
    if not root.is_dir():
        return pkgs  # a tree that does not exist yet has no packages
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (d / "rust2rpm.toml").exists():
            continue
        crate, version, name = d.name, None, None
        specs = sorted(d.glob("rust-*.spec"), key=lambda f: f.stat().st_mtime)
        if len(specs) > 1:
            util.warn(
                f"{d.name}: several specs ({', '.join(f.name for f in specs)}); using the newest"
            )
        for spec in specs[-1:]:
            text = spec.read_text()
            name = spec.stem
            if m := re.search(r"^%global crate\s+(\S+)", text, re.M):
                crate = m.group(1)
            if m := re.search(r"^Version:\s+(\S+)", text, re.M):
                version = m.group(1)
        pkgs[crate] = LocalPackage(crate, d, version, name)
    return pkgs


def select(root: Path, names: list[str], all_: bool) -> list[LocalPackage]:
    pkgs = local_packages(root)
    if all_:
        if not pkgs:
            util.die(
                f"no packages under {root} (pass --root DIR or set {config.ROOT_ENV})"
            )
        return list(pkgs.values())
    if not names:
        util.die("name at least one crate, or pass --all")
    out = []
    for n in names:
        if n not in pkgs:
            util.die(
                f"{n}: no package in {root / n} (wrong --root? or run 'rust-deps init {n}' first)"
            )
        out.append(pkgs[n])
    return out
