"""rust-deps resolve: which crates are missing and how to get them."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import collections
import json

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
    status: str  # "new" | "update" (Fedora has the crate, but no matching version)
    fedora_versions: list[str]
    needed_by: set[str] = field(default_factory=set)
    features: set[str] = field(default_factory=set)
    missing_optional: list[str] = field(default_factory=list)
    missing_dev: list[str] = field(default_factory=list)
    missing_features: list[str] = field(default_factory=list)


def resolve(
    seeds: list[tuple[str, str, str, list[str]]],
    index: fedora.FedoraIndex,
    local: dict[str, packages.LocalPackage],
    include_dev_seeds: bool = True,
) -> dict[str, Needed]:
    """Find every crate that must be packaged for the seeds to build.

    seeds: (crate, req, needed_by, features).  Recurses through normal and
    build dependencies (including optional ones enabled by requested/default
    features); dev and other optional dependencies are only reported.
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
                n.missing_features = sorted(set(n.missing_features) | set(miss))
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
            if new_feats <= n.features:
                continue
        else:
            status = "update" if index.versions(name) else "new"
            n = needed[key] = Needed(name, v["num"], status, index.versions(name))
            n.needed_by.add(why)
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


def seeds_from_manifest(path: Path) -> list[tuple[str, str, str, list[str]]]:
    res = util.run(
        [
            "cargo",
            "metadata",
            "--no-deps",
            "--format-version",
            "1",
            "--manifest-path",
            str(path),
        ],
        capture_output=True,
    )
    if res.returncode != 0:
        util.die(f"cargo metadata failed:\n{res.stderr}")
    meta = json.loads(res.stdout)
    target = str(path.resolve())
    pkgs = [p for p in meta["packages"] if p["manifest_path"] == target] or meta[
        "packages"
    ]
    seeds = []
    for p in pkgs:
        for d in p["dependencies"]:
            if d.get("path") and not d.get("req", "").strip("^*"):
                continue
            if versions.is_foreign(d.get("target")):
                continue
            feats = list(d["features"]) + (
                ["default"] if d["uses_default_features"] else []
            )
            seeds.append((d["name"], d["req"], p["name"], feats))
    return seeds


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
        seeds += seeds_from_manifest(Path(m))
    if not seeds:
        util.die("give crate names and/or --manifest")
    needed = resolve(seeds, index, local)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        **n.__dict__,
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
