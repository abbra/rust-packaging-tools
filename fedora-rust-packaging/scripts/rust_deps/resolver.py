"""rust-deps resolve: which crates are missing and how to get them."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import collections
import json
import tomllib

from . import archives
from . import cratesio
from . import fedora
from . import packages
from . import util
from . import versions


@dataclass
class Needed:
    crate: str
    version: str
    status: str  # "new" | "update" | "features" | "fedora" | "local"
    fedora_versions: list[str]
    reqs: set[str] = field(default_factory=set)
    needed_by: set[str] = field(default_factory=set)
    features: set[str] = field(default_factory=set)
    missing_optional: list[str] = field(default_factory=list)
    missing_dev: list[str] = field(default_factory=list)
    missing_features: list[str] = field(default_factory=list)


@dataclass
class Member:
    """One crate of a Cargo workspace, built from its source directory."""

    crate: str
    version: str
    manifest: Path


@dataclass
class Requirement:
    """A dependency one member of a workspace asks for.

    source: "registry" (crates.io), "workspace" (another member, so the source
    tree provides it) or "path" (a local directory outside the workspace).
    """

    crate: str
    req: str
    kind: str  # "normal" | "dev" | "build"
    features: list[str]
    required_by: str
    source: str
    path: str | None = None
    optional: bool = False
    default_enabled: bool = False  # enabled by the member's default features
    enabled_by: list[str] = field(default_factory=list)  # features that would enable it


@dataclass
class Manifest:
    """A Cargo.toml, the workspace around it, and what it needs from outside."""

    path: Path
    root: Path
    members: list[Member]
    scope: list[Member]  # the members the request was about
    requirements: list[Requirement]

    @property
    def workspace(self) -> bool:
        return len(self.members) > 1

    @property
    def seeds(self) -> list[tuple[str, str, str, list[str]]]:
        """What the scope asks for from crates.io, as resolve() seeds.

        Workspace members and local path dependencies are not asked of
        crates.io: they are not published there, and the source tree has them.
        Optional dependencies no enabled feature asks for are left out, as
        resolve() does for the dependencies of the crates it walks.
        """
        return [
            (r.crate, r.req, r.required_by, r.features)
            for r in self.requirements
            if r.source == "registry" and (not r.optional or r.default_enabled)
        ]

    def optional_requirements(self) -> list[Requirement]:
        """Registry dependencies only a feature the members do not enable asks for."""
        return [
            r
            for r in self.requirements
            if r.source == "registry" and r.optional and not r.default_enabled
        ]

    def dev_only(self) -> set[str]:
        """Crates only a member's tests need."""
        dev = {r.crate for r in self.requirements if r.kind == "dev"}
        return dev - {r.crate for r in self.requirements if r.kind != "dev"}

    def notice(self) -> str:
        """What answering about a workspace member means for the result."""
        if len(self.scope) == len(self.members):
            return (
                f"Workspace {self.root}: {len(self.members)} member crates; their sibling requirements are satisfied by the source tree, not by crates.io."
            )
        return (
            f"{self.scope[0].crate} is a member of the workspace at {self.root} ({len(self.members)} crates); its sibling requirements are satisfied by the source tree, not by crates.io."
        )


def manifest_file(path: Path) -> Path:
    """The Cargo.toml of a project: a directory means the one in it."""
    if path.is_dir():
        path = path / "Cargo.toml"
    if not path.is_file():
        util.die(
            f"{path}: no Cargo.toml there (give the project directory or its manifest)"
        )
    return path


def workspace_root(manifest: Path) -> Path:
    """The directory whose Cargo.toml declares the [workspace] a manifest is in.

    A member manifest's [workspace.metadata] is not such a declaration; without
    one, the manifest's own directory is the root.
    """
    for d in [manifest.parent, *manifest.parent.parents]:
        f = d / "Cargo.toml"
        if not f.is_file():
            continue
        try:
            table = tomllib.loads(f.read_text()).get("workspace")
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        if isinstance(table, dict) and set(table) - {"metadata"}:
            return d
    return manifest.parent


def cargo_metadata(manifest: Path) -> dict:
    res = util.run(
        [
            "cargo",
            "metadata",
            "--no-deps",
            "--format-version",
            "1",
            "--manifest-path",
            str(manifest),
        ],
        capture_output=True,
    )
    if res.returncode != 0:
        util.die(f"cargo metadata failed for {manifest}:\n{res.stderr}")
    return json.loads(res.stdout)


def read_metadata(meta: dict, asked: Path) -> Manifest:
    """The workspace and the outside requirements of parsed 'cargo metadata' output.

    'cargo metadata' reports the whole workspace whichever member it is asked
    about; the scope is the manifest that was given, or every member when it is
    the workspace root.
    """
    member_ids = set(meta.get("workspace_members") or [])
    described = [
        p for p in meta.get("packages") or [] if not member_ids or p.get("id") in member_ids
    ]
    members = [
        Member(p["name"], p.get("version") or "", Path(p["manifest_path"]))
        for p in described
    ]
    if not members:
        util.die(f"{asked}: 'cargo metadata' described no package there")
    # a path dependency names the directory of the crate it points at
    inside: dict[Path, Member] = {}
    for m in members:
        inside[m.manifest.resolve()] = m
        inside[m.manifest.parent.resolve()] = m
    root = workspace_root(asked)
    scope = (
        list(members)
        if asked.resolve() == (root / "Cargo.toml").resolve()
        else [m for m in members if m.manifest.resolve() == asked.resolve()] or members
    )
    scoped = {m.manifest.resolve() for m in scope}
    requirements: list[Requirement] = []
    for p in described:
        if Path(p["manifest_path"]).resolve() not in scoped:
            continue
        declared = p.get("features") or {}
        _, enabled = archives.feature_closure(declared, {"default"})
        for d in p.get("dependencies") or []:
            if versions.is_foreign(d.get("target")):
                continue
            path = d.get("path")
            optional = bool(d.get("optional"))
            requirements.append(
                Requirement(
                    crate=d["name"],
                    req=d.get("req") or "*",
                    kind=d.get("kind") or "normal",
                    features=list(d.get("features") or [])
                    + (["default"] if d.get("uses_default_features") else []),
                    required_by=p["name"],
                    source="workspace"
                    if path and Path(path).resolve() in inside
                    else "path"
                    if path
                    else "registry",
                    path=path,
                    optional=optional,
                    default_enabled=d["name"] in enabled,
                    enabled_by=sorted(
                        f
                        for f in declared
                        if f != "default"
                        and d["name"] in archives.feature_closure(declared, {f})[1]
                    )
                    if optional
                    else [],
                )
            )
    return Manifest(
        path=asked, root=root, members=members, scope=scope, requirements=requirements
    )


def read_manifest(path: Path) -> Manifest:
    """Read a project's Cargo.toml and the Rust workspace around it."""
    asked = manifest_file(path)
    return read_metadata(cargo_metadata(asked), asked)


def resolve(
    seeds: list[tuple[str, str, str, list[str]]],
    index: fedora.FedoraIndex,
    local: dict[str, packages.LocalPackage],
    include_dev_seeds: bool = True,
    report_provided: bool = False,
) -> dict[str, Needed]:
    """Find every crate that must be packaged for the seeds to build.

    seeds: (crate, req, needed_by, features).  Recurses through normal and
    build dependencies (including optional ones enabled by requested/default
    features); dev and other optional dependencies are only reported.
    With report_provided, the crates Fedora or the local package tree already
    provides are reported too (status "fedora" / "local"): a project must take
    those from the packages instead of vendoring them.
    """
    needed: dict[str, Needed] = {}
    todo = collections.deque(seeds)
    while todo:
        name, req, why, feats = todo.popleft()
        if (
            name in local
            and local[name].version
            and versions.req_matches(req, local[name].version)
        ):
            if report_provided:
                _provided(needed, name, local[name].version, "local", [], req, why, feats)
            continue
        fv = index.best(name, req)
        if fv:
            miss = index.missing_features(
                name, fv, [f for f in feats if f != "default"]
            )
            if miss:
                key = f"{name}@{fv}"
                n = needed.setdefault(
                    key, Needed(name, fv, "features", index.versions(name))
                )
                n.needed_by.add(why)
                n.reqs.add(req)
                n.missing_features = sorted(set(n.missing_features) | set(miss))
            elif report_provided:
                _provided(needed, name, fv, "fedora", index.versions(name), req, why, feats)
            continue
        v = cratesio.pick_version(name, req)
        if v is None:
            util.warn(f"{name} {req}: no matching release on crates.io")
            continue
        key = f"{name}@{v['num']}"
        n = needed.get(key)
        new_feats = set(feats) | {"default"}
        if n is not None:
            n.needed_by.add(why)
            n.reqs.add(req)
            if new_feats <= n.features:
                continue
        else:
            status = "update" if index.versions(name) else "new"
            n = needed[key] = Needed(name, v["num"], status, index.versions(name))
            n.needed_by.add(why)
            n.reqs.add(req)
        n.features |= new_feats
        _, enabled = archives.feature_closure(v.get("features") or {}, n.features)
        n.missing_optional, n.missing_dev = [], []
        for d in cratesio.crate_dependencies(name, v["num"]):
            if versions.is_foreign(d.get("target")):
                continue
            dn, dreq = d["crate_id"], d["req"]
            available = index.best(dn, dreq) or (
                dn in local
                and local[dn].version
                and versions.req_matches(dreq, local[dn].version)
            )
            dfeats = list(d["features"]) + (
                ["default"] if d["default_features"] else []
            )
            if d["kind"] == "dev":
                if not available:
                    n.missing_dev.append(f"{dn} {dreq}")
                continue
            if d["optional"] and dn not in enabled:
                if not available:
                    n.missing_optional.append(f"{dn} {dreq}")
                continue
            todo.append((dn, dreq, name, dfeats))
    return needed


def _provided(
    needed: dict[str, Needed],
    name: str,
    version: str,
    status: str,
    fedora_versions: list[str],
    req: str,
    why: str,
    feats: list[str],
) -> None:
    key = f"{name}@{version}"
    n = needed.setdefault(key, Needed(name, version, status, fedora_versions))
    n.needed_by.add(why)
    n.reqs.add(req)
    n.features |= set(feats)


def cmd_resolve(args) -> None:
    index = fedora.fedora_index(args.refresh, args.target)
    local = {} if args.ignore_local else packages.local_packages(args.root)
    for extra in args.local_root or []:
        local = {**packages.local_packages(Path(extra)), **local}
    seeds = []
    for spec in args.crates:
        name, _, req = spec.partition("@")
        seeds.append((name, req or "*", "(command line)", ["default"]))
    for m in args.manifest or []:
        manifest = read_manifest(Path(m))
        if manifest.workspace:
            util.info(manifest.notice())
        seeds += manifest.seeds
    if not seeds:
        util.die("give crate names and/or --manifest")
    needed = resolve(seeds, index, local)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        **n.__dict__,
                        "reqs": sorted(n.reqs),
                        "needed_by": sorted(n.needed_by),
                        "features": sorted(n.features),
                    }
                    for n in needed.values()
                ],
                indent=2,
            )
        )
        return
    if not needed:
        util.info("Everything is available in Fedora or in the local package tree.")
        return
    for n in needed.values():
        label = {"new": "NEW", "update": "UPDATE", "features": "FEATURES"}[n.status]
        extra = (
            f" (Fedora has {', '.join(n.fedora_versions)})" if n.fedora_versions else ""
        )
        util.info(
            f"{label:9} {n.crate} {n.version}{extra}  <- {', '.join(sorted(n.needed_by))}"
        )
        if n.missing_features:
            util.info(
                f"            missing features in Fedora package: {', '.join(n.missing_features)}"
            )
        if n.missing_optional:
            util.info(
                f"            optional deps not in Fedora: {', '.join(n.missing_optional)}"
            )
        if n.missing_dev:
            util.info(
                f"            dev deps not in Fedora:      {', '.join(n.missing_dev)}"
            )
    util.info("")
    util.info(
        "NEW: package it with 'rust-deps init <crate>' (or 'init --recursive' for everything)."
    )
    util.info(
        "UPDATE: Fedora ships another version; update that package or create a compat package."
    )
    util.info(
        "FEATURES: the Fedora package lacks feature subpackages (check its Cargo.toml patch)."
    )
