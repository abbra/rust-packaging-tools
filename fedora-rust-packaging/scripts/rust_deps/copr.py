"""rust-deps COPR build diagnosis: state, conflicts, missing requirements."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import collections
import gzip
import json
import re
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from . import archives
from . import config
from . import fedora
from . import packages
from . import regen
from . import reviewrequest
from . import reviewstatus
from . import util
from . import versions

COPR_OK = {"succeeded", "forked", "skipped"}  # skipped: ExclusiveArch/ExcludeArch
COPR_ACTIVE = {"importing", "pending", "starting", "running", "waiting"}

# dnf5: "Problem: nothing provides requested (crate(x/default) >= 1.0.0 with crate(x/default) < 2.0.0~)"
# dnf4: " Problem 1: nothing provides requested (...)", "No matching package to install: 'x'"
MISSING_RE = re.compile(
    r"nothing provides (?:requested )?(.+?)(?: needed by \S+.*)?\s*$"
    r"|No match(?:ing package)? (?:for argument|to install):? '?(.+?)'?\s*$"
)
CRATE_REQ_RE = re.compile(
    r"crate\(([^/)\s]+)(?:/([^)\s]+))?\)(?:\s*(>=|=|>)\s*([^\s)]+))?"
    r"(?:\s+with\s+crate\([^)]*\)\s*(<=|<)\s*([^\s)]+))?"
)
# dnf4/dnf5: "cannot install both rust-x-devel-0.16.1-1.el10.noarch from epel and rust-x-devel-0.17.1-1.el10.noarch from copr_base"
CONFLICT_RE = re.compile(r"cannot install both (\S+) from (\S+) and (\S+) from (\S+)")
COPR_REPO = "copr_base"  # the project's own repository in COPR's build roots


@dataclass
class Conflict:
    """Two versions of one package that a build needs together but cannot install."""

    name: str  # binary package, e.g. rust-hashbrown-devel
    crate: str  # e.g. hashbrown
    sides: list[tuple[str, str]]  # (version, repository)

    @classmethod
    def parse(cls, m: re.Match) -> "Conflict | None":
        nevra1, repo1, nevra2, repo2 = m.groups()
        n1, v1, _ = nevra1.rsplit("-", 2)
        n2, v2, _ = nevra2.rsplit("-", 2)
        crate = re.fullmatch(r"rust-(.+?)(?:\+[^+]+)?-devel", n1)
        if n1 != n2 or not crate:
            return None
        return cls(n1, crate.group(1), [(v1, repo1), (v2, repo2)])

    def label(self) -> str:
        return (
            f"{self.name} {self.sides[0][0]} ({self.sides[0][1]}) and {self.sides[1][0]} "
            f"({self.sides[1][1]}) cannot be installed together"
        )


def log_conflicts(lines: list[str]) -> list[Conflict]:
    out: dict[str, Conflict] = {}
    for ln in lines:
        if (m := CONFLICT_RE.search(ln)) and (c := Conflict.parse(m)):
            out.setdefault(c.label(), c)
    return list(out.values())


BUILD_ERROR_RE = re.compile(
    r"^(error(\[E\d+\])?: .*|test result: FAILED.*|thread '.*' panicked at .*"
    r"|---- \S+ stdout ----|error: Bad exit status.*)$"
)


def copr_builds_file(project: str, crate: str) -> Path:
    return (
        config.CACHE_DIR / "copr-builds" / project.replace("/", "_") / f"{crate}.json"
    )


def record_copr_build(project: str, crate: str, build_id: int) -> None:
    """Remember a submitted build: COPR files it under its package only after
    importing the SRPM, so until then a package's build list misses it."""
    f = copr_builds_file(project, crate)
    ids = json.loads(f.read_text()) if f.exists() else []
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(sorted(set(ids) | {build_id})[-20:]))


def copr_api(path: str, **params) -> dict:
    return json.loads(
        reviewrequest._get(
            f"{reviewrequest.COPR_URL}/api_3/{path}?{urllib.parse.urlencode(params)}"
        )
    )


def spec_runs_check(spec: Path) -> bool:
    """Whether the spec's %check section runs (as the dev-dependencies then are BuildRequires).

    rpmspec expands the spec, so every form counts: %bcond check 0|1, the older
    %bcond_with check (off by default) and %bcond_without check (on), and
    expressions (evaluated on this host).  Without rpmspec, read the literal forms.
    """
    res = util.run(["rpmspec", "-P", str(spec)], capture_output=True)
    if res.returncode == 0:
        return bool(re.search(r"^%check\b", res.stdout, re.M))
    text = spec.read_text()
    if m := re.search(r"^%bcond\s+check\s+(\d+)\s*$", text, re.M):
        return m.group(1) != "0"
    if re.search(r"^%bcond_with\s+check\b", text, re.M):
        return False
    return bool(re.search(r"^%check\b", text, re.M))


_BUILD_INFO: dict[tuple[str, str | None], tuple[dict, list[str]]] = {}


def build_info(pkg: packages.LocalPackage) -> tuple[dict, list[str]]:
    """The patched Cargo.toml and the BuildRequires rust2rpm generates from it.

    Runs cargo2rpm like %cargo_generate_buildrequires in the spec, with its
    feature flags, and with dev-dependencies unless the spec disables %check.
    """
    key = (str(pkg.dir), pkg.version)
    if key not in _BUILD_INFO:
        spec = pkg.spec.read_text()
        m = re.search(r"^%cargo_generate_buildrequires(.*)$", spec, re.M)
        flags = (m.group(1) if m else "").split()
        with_check = "-t" in flags or spec_runs_check(pkg.spec)
        args = regen.cargo_feature_flags(flags)
        with tempfile.TemporaryDirectory() as tmp:
            src = archives.extract_patched(pkg, Path(tmp))
            toml = tomllib.loads((src / "Cargo.toml").read_text())
            res = util.run(
                [
                    "cargo2rpm",
                    "--path",
                    "Cargo.toml",
                    "buildrequires",
                    *args,
                    *(["--with-check"] if with_check else []),
                ],
                cwd=src,
                capture_output=True,
            )
        if res.returncode:
            util.die(f"{pkg.crate}: cargo2rpm buildrequires failed:\n{res.stderr}")
        _BUILD_INFO[key] = (toml, res.stdout.split("\n"))
    return _BUILD_INFO[key]


def dep_kinds(pkg: packages.LocalPackage) -> dict[str, set[str]]:
    """crate -> kinds (normal, dev, build) in the package's patched Cargo.toml."""
    kinds: dict[str, set[str]] = collections.defaultdict(set)
    if pkg.crate_file and pkg.crate_file.exists():
        for d in archives.manifest_deps(build_info(pkg)[0]):
            kinds[d["crate_id"]].add(d["kind"])
    return kinds


def provided_features(toml: dict) -> set[str]:
    """Features a package built from this Cargo.toml provides (crate(name/feature))."""
    feats = toml.get("features", {})
    explicit = {
        i[4:] for items in feats.values() for i in items if i.startswith("dep:")
    }
    optional = {
        d["name"]
        for d in archives.manifest_deps(toml)
        if d["optional"] and d["kind"] == "normal"
    }
    return set(feats) | {"", "default"} | (optional - explicit)


def target_availability(
    idx: fedora.FedoraIndex, rel: str, req: "MissingReq"
) -> tuple[str | None, str]:
    """(the target's version that satisfies req, or None and why not)."""
    have = idx.versions(req.crate)
    fitting = [v for v in have if versions.req_matches(req.req, v)]
    lacking = (
        idx.missing_features(req.crate, fitting[-1], req.features) if fitting else []
    )
    if fitting and not lacking:
        return fitting[-1], ""
    if fitting:
        return None, f"{rel} has {fitting[-1]} without feature(s) {', '.join(lacking)}"
    if have:
        return None, f"{rel} has only {', '.join(have)}"
    return None, f"not in {rel}"


_TARGET_INDEX: dict[str, fedora.FedoraIndex] = {}


def target_index(chroot: str, refresh: bool = False) -> fedora.FedoraIndex:
    """The crate index of a chroot's release (shared by all its architectures)."""
    rel = (
        fedora.chroot_release(chroot)
        if re.search(r"-(\d+|rawhide)-[^-]+$", chroot)
        else chroot
    )
    if rel not in _TARGET_INDEX:
        _TARGET_INDEX[rel] = fedora.fedora_index(refresh, f"{rel}-x86_64")
    return _TARGET_INDEX[rel]


def mock_chroot(chroot: str) -> str:
    """A mock config to reproduce a COPR chroot locally: RHEL needs a
    subscription, CentOS Stream is the closest (its EPEL is epel-N, not epel-z-N)."""
    return "centos-stream+" + chroot[5:] if chroot.startswith("rhel+") else chroot


@dataclass
class MissingReq:
    text: str  # as dnf printed it
    crate: str | None = None  # None for a non-crate requirement (e.g. pkgconfig(...))
    features: list[str] = field(default_factory=list)  # "" for crate(name) itself
    req: str = "*"  # cargo syntax, e.g. ">=1.0.23, <2.0.0"
    tests_only: bool = False  # only dev-dependencies need it

    @classmethod
    def parse(cls, text: str) -> "MissingReq":
        text = text.strip()
        m = CRATE_REQ_RE.search(text)
        if not m:
            return cls(text)
        name, feat, op, lo, op_hi, hi = m.groups()
        parts = [f"{op}{lo.replace('~', '-')}"] if op else []
        if (
            hi
        ):  # "< 2.0.0~" (a caret requirement), or "<= 0.8.0" (as a spec may relax it)
            parts.append(f"{op_hi}{hi.rstrip('~').replace('~', '-')}")
        return cls(text, name, [feat or ""], ", ".join(parts) or "*")

    def key(self) -> tuple:
        return (self.crate, self.req) if self.crate else (self.text,)

    def label(self) -> str:
        if not self.crate:
            return self.text
        feats = [f for f in self.features if f]
        return f"{self.crate} {self.req}" + (f" [{', '.join(feats)}]" if feats else "")


@dataclass
class ChrootDiag:
    chroot: str
    state: str
    verdict: str  # ok | active | wait | retry | blocked | error
    notes: list[str] = field(default_factory=list)
    log: Path | None = None
    log_url: str | None = None
    via: set[str] = field(default_factory=set)  # local deps this retry relies on
    external: list[tuple[MissingReq, str]] = field(
        default_factory=list
    )  # missing outside the tree, why
    build: int | None = None  # the build this result is from


@dataclass
class PackageDiag:
    pkg: packages.LocalPackage
    evr: str
    build: dict | None
    chroots: dict[str, ChrootDiag]


VERDICT_RANK = {"ok": 0, "retry": 1, "wait": 2, "active": 2, "error": 3, "blocked": 4}


class CoprState:
    """Latest COPR build of each local package's current version, diagnosed per chroot."""

    def __init__(self, project: str, root: Path, refresh: bool = False):
        self.project = project
        self.owner, _, self.name = project.partition("/")
        self.local = packages.local_packages(root)
        self.refresh = refresh
        self._diag: dict[str, PackageDiag] = {}
        try:
            self.project_chroots = sorted(
                copr_api("project/", ownername=self.owner, projectname=self.name)[
                    "chroot_repos"
                ]
            )
        except urllib.error.HTTPError as exc:
            util.die(f"cannot read COPR project {project}: {exc}")

    def diagnosed(self) -> dict[str, PackageDiag]:
        """Every package diagnosed so far, including dependencies outside the selection."""
        return self._diag

    def index(self, chroot: str) -> fedora.FedoraIndex:
        return target_index(chroot, self.refresh)

    def dep_kinds(self, pkg: packages.LocalPackage) -> dict[str, set[str]]:
        return dep_kinds(pkg)

    def builds(self, pkg: packages.LocalPackage, evr: str) -> list[dict]:
        """Builds of this version, newest first."""
        try:
            items = copr_api(
                "build/list/",
                ownername=self.owner,
                projectname=self.name,
                packagename=pkg.rpm_name,
                limit=100,
            )["items"]
        except urllib.error.HTTPError:
            items = []
        f = copr_builds_file(self.project, pkg.crate)
        known = {b["id"] for b in items}
        for bid in (json.loads(f.read_text()) if f.exists() else []):
            if bid not in known and bid > max(known, default=0):
                try:
                    items.append(copr_api(f"build/{bid}"))
                except urllib.error.HTTPError:
                    pass  # deleted

        def ours(
            b: dict,
        ) -> bool:  # this version of this package (a compat package has its own name)
            src = b.get("source_package") or {}
            if src.get("version") is None:  # not imported yet
                return b["state"] in COPR_ACTIVE
            return src.get("version") == evr and src.get("name") in (None, pkg.rpm_name)

        return [
            b for b in sorted(items, key=lambda b: b["id"], reverse=True) if ours(b)
        ]

    def diagnose(self, pkg: packages.LocalPackage) -> PackageDiag:
        if pkg.crate in self._diag:
            return self._diag[pkg.crate]
        evr = reviewrequest.spec_query(pkg.spec, "%{version}-%{release}")
        builds = self.builds(pkg, evr)
        diag = PackageDiag(pkg, evr, builds[0] if builds else None, {})
        self._diag[pkg.crate] = diag
        # each chroot from the newest build that has it: a retry covers only
        # the chroots that failed, so older builds hold the others' results
        for build in builds:
            for bc in copr_api("build-chroot/list/", build_id=build["id"])["items"]:
                if bc["name"] not in diag.chroots and pkg.builds_in(bc["name"]):
                    diag.chroots[bc["name"]] = self.diagnose_chroot(pkg, build, bc)
                    diag.chroots[bc["name"]].build = build["id"]
        if not builds:  # never built: its local dependencies may need a build too
            for dep in sorted(set(self.dep_kinds(pkg)) & set(self.local)):
                if dep != pkg.crate and self.local[dep].version:
                    self.diagnose(self.local[dep])
        return diag

    def diagnose_chroot(
        self, pkg: packages.LocalPackage, build: dict, bc: dict
    ) -> ChrootDiag:
        chroot, state = bc["name"], bc["state"]
        if state in COPR_OK:
            return ChrootDiag(chroot, state, "ok")
        if state in COPR_ACTIVE:
            return ChrootDiag(chroot, state, "active")
        if state == "canceled":
            return ChrootDiag(chroot, state, "retry", ["canceled"])
        d = ChrootDiag(chroot, state, "error")
        if not bc.get("result_url"):
            d.notes.append("no build results; see the build page")
            return d
        d.log_url = bc["result_url"].rstrip("/") + "/builder-live.log.gz"
        d.log = self.fetch_log(build["id"], chroot, bc["result_url"])
        if not d.log:
            d.notes.append("build log not available")
            return d
        text = d.log.read_text(errors="replace")
        missing: dict[tuple, MissingReq] = (
            {}
        )  # features of one crate requirement together
        for ln in text.splitlines():
            if m := MISSING_RE.search(ln):
                req = MissingReq.parse(m.group(1) or m.group(2))
                have = missing.setdefault(req.key(), req)
                have.features += [f for f in req.features if f not in have.features]
        conflicts = log_conflicts(text.splitlines())
        if conflicts and not missing:
            verdicts = [self.check_conflict(pkg, chroot, c, d) for c in conflicts]
            d.verdict = max(verdicts, key=VERDICT_RANK.__getitem__)
            return d
        if not missing:
            errors = list(
                dict.fromkeys(
                    ln.strip()
                    for ln in text.splitlines()
                    if BUILD_ERROR_RE.match(ln.strip())
                )
            )
            d.notes += [f"build error: {e}" for e in errors[:8]] or [
                "failed; no known error pattern in the log"
            ]
            if len(errors) > 8:
                d.notes.append(f"… {len(errors) - 8} more error line(s) in the log")
            return d
        verdicts = [self.check_missing(pkg, chroot, req, d) for req in missing.values()]
        d.verdict = max(verdicts, key=VERDICT_RANK.__getitem__)
        return d

    def copr_has_package(self, name: str) -> bool:
        try:
            copr_api(
                "package/",
                ownername=self.owner,
                projectname=self.name,
                packagename=name,
            )
            return True
        except urllib.error.HTTPError:
            return False

    def check_conflict(
        self, pkg: packages.LocalPackage, chroot: str, c: Conflict, d: ChrootDiag
    ) -> str:
        """Explain a version conflict in the build root; returns the verdict it implies."""
        label = f"conflict: {c.label()}"
        base = c.name.removesuffix("-devel").split("+")[0]  # rust-hashbrown
        local = next(
            (
                lp
                for lp in self.local.values()
                if base in (f"rust-{lp.crate}", lp.rpm_name)
            ),
            None,
        )
        if not local or COPR_REPO not in (repo for _, repo in c.sides):
            d.notes.append(
                f"{label}: both come from the target; the build needs two versions of it"
            )
            return "blocked"
        if not local.compat:
            d.notes.append(
                f"{label}: the build needs both versions, and the local {local.crate} package has the "
                f"same name as the target's. Make it a compat package: regen --compat {local.crate}, "
                f"srpm, delete the old one from COPR (copr-cli delete-package {self.project} "
                f"--name {base}), then copr --retry-failed"
            )
            return "blocked"
        if self.copr_has_package(base):
            d.notes.append(
                f"{label}: the local {local.crate} is now the compat package {local.rpm_name}, but "
                f"COPR still has {base}, which builds can pick; delete it (copr-cli delete-package "
                f"{self.project} --name {base}) and resubmit"
            )
            return "blocked"
        dd = self.diagnose(local)
        dc = dd.chroots.get(chroot)
        if dc and dc.verdict == "ok":
            d.notes.append(f"{label}: now {local.rpm_name}, built there; resubmit")
            return "retry"
        if dc and dc.verdict in ("active", "wait"):
            d.notes.append(f"{label}: now {local.rpm_name}, still building there; wait")
            return "wait"
        d.notes.append(f"{label}: now {local.rpm_name}; submit it, then this")
        d.via.add(local.crate)
        return "retry"

    def check_missing(
        self, pkg: packages.LocalPackage, chroot: str, req: MissingReq, d: ChrootDiag
    ) -> str:
        """Explain one missing BuildRequires; returns the verdict it implies."""
        label = f"missing {req.label()}"
        if not req.crate:
            d.notes.append(
                f"{label}: not a crate; check that {fedora.chroot_release(chroot)} ships it"
            )
            d.external.append((req, "not a crate"))
            return "blocked"
        kinds = self.dep_kinds(pkg).get(req.crate, set())
        if kinds and kinds <= {"dev"}:
            req.tests_only = True
            label += " (dev-dependency: only the tests need it)"
        dep = self.local.get(req.crate)
        if dep and not dep.builds_in(chroot):
            dep = None  # the target's own package is meant to serve it
        if dep and dep.version and versions.req_matches(req.req, dep.version):
            dd = self.diagnose(dep)
            if not dd.build:
                d.notes.append(
                    f"{label}: local {dep.crate} {dd.evr} has no COPR build; submit it"
                )
                d.via.add(dep.crate)
                return "retry"
            dc = dd.chroots.get(chroot)
            bid = dc.build if dc else dd.build["id"]
            blink = util.link(reviewstatus.copr_build_url(bid), str(bid))
            if dc is None:
                d.notes.append(
                    f"{label}: local {dep.crate} build {blink} did not include {chroot}; submit it there"
                )
                d.via.add(dep.crate)
                return "retry"
            if dc.verdict == "ok":
                d.notes.append(
                    f"{label}: local {dep.crate} built there since (build {blink}); resubmit"
                )
                return "retry"
            if dc.verdict in ("active", "wait"):
                d.notes.append(
                    f"{label}: local {dep.crate} is still building there (build {blink}); wait"
                )
                return "wait"
            if dc.verdict == "retry":
                d.notes.append(
                    f"{label}: local {dep.crate} failed there too, and can be resubmitted first"
                )
                d.via.add(dep.crate)
                return "retry"
            d.notes.append(
                f"{label}: local {dep.crate} failed there too (build {blink}); fix it first"
            )
            return "blocked"
        rel = fedora.chroot_release(chroot)
        version, why = target_availability(self.index(chroot), rel, req)
        if version:
            d.notes.append(f"{label}: {rel} has {version} now; resubmit")
            return "retry"
        if dep:
            why += f"; the local package is {dep.version or 'not generated'}"
        d.notes.append(f"{label}: {why}")
        d.external.append((req, why))
        return "blocked"

    @staticmethod
    def fetch_log(build_id: int, chroot: str, result_url: str) -> Path | None:
        """The build log of a finished chroot, cached (it never changes)."""
        path = config.CACHE_DIR / "copr-logs" / f"{build_id}-{chroot}.log"
        if path.exists():
            return path
        base = result_url.rstrip("/")
        for name, unpack in (
            ("builder-live.log.gz", gzip.decompress),
            ("builder-live.log", lambda b: b),
        ):
            try:
                data = unpack(reviewrequest._get(f"{base}/{name}"))
            except (urllib.error.URLError, OSError, EOFError):
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return path
        return None
