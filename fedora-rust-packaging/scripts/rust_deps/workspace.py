"""rust-deps workspace: what a Rust workspace needs, and where each crate comes from.

A workspace builds its member crates from its own source tree; they are
normally not published on crates.io and are never asked of it.  Everything the
members do ask for should come from Fedora's crate packages instead of a
vendor/ directory, so this says which crates Fedora already ships (take them
from the packages), which are missing (package them), and which of a vendor/
tree can go away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import collections
import json
import tomllib

from . import fedora
from . import packages
from . import resolver
from . import util


# What each answer means for the project's spec.
LABELS = {
    "fedora": "SYSTEM",
    "local": "PACKAGED",
    "features": "FEATURES",
    "update": "UPDATE",
    "new": "NEW",
    "workspace": "WORKSPACE",
    "path": "PATH",
    "git": "GIT",
    "alt": "ALTREG",
    "optional": "OPTIONAL",
}
ORDER = [
    "fedora",
    "local",
    "features",
    "update",
    "new",
    "workspace",
    "path",
    "git",
    "alt",
    "optional",
]
ADVICE = {
    "fedora": "Fedora ships this version: the spec BuildRequires crate(<name>); do not vendor it.",
    "local": "already packaged in the packages root; build against it.",
    "features": "Fedora's package lacks a feature the workspace asks for; check its Cargo.toml patch.",
    "update": "Fedora ships another version; update that package, make a compat package, or keep it vendored.",
    "new": "not in Fedora; package it with 'rust-deps init' or keep it vendored.",
    "workspace": "a member of the workspace; the source tree provides it.",
    "path": "a path dependency outside the workspace; it comes from that directory.",
    "git": "a dependency from a Git repository; not a crates.io crate, so no Fedora package provides it.",
    "alt": "a dependency from an alternate registry; not a crates.io crate, so no Fedora package provides it.",
    "optional": "only a feature the members do not enable asks for it; nothing to do by default.",
}
SUMMARY = {
    "fedora": "from Fedora's packages",
    "local": "already packaged here",
    "features": "need a feature Fedora's package lacks",
    "update": "need a version Fedora does not ship",
    "new": "not in Fedora",
    "workspace": "workspace members",
    "path": "local path dependencies",
    "git": "from a Git repository",
    "alt": "from an alternate registry",
    "optional": "only for a feature that is not enabled",
}


@dataclass
class Item:
    """One crate the workspace needs, and where it comes from."""

    crate: str
    status: str
    version: str = ""
    reqs: list[str] = field(default_factory=list)
    needed_by: list[str] = field(default_factory=list)
    fedora_versions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def vendored_crates(root: Path) -> list[tuple[str, str]]:
    """(crate, version) of the crates a 'cargo vendor' tree holds."""
    out = []
    for d in sorted((root / "vendor").glob("*/Cargo.toml")):
        try:
            pkg = tomllib.loads(d.read_text()).get("package") or {}
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        if pkg.get("name"):
            out.append((pkg["name"], str(pkg.get("version", ""))))
    return out


def replaced_source(root: Path) -> str | None:
    """The source a .cargo/config.toml replaces with vendored crates, if any."""
    for cfg in (root / ".cargo/config.toml", root / ".cargo/config"):
        if not cfg.is_file():
            continue
        try:
            sources = tomllib.loads(cfg.read_text()).get("source") or {}
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        for name, spec in sources.items():
            if isinstance(spec, dict) and spec.get("replace-with"):
                return name
    return None


def items(
    manifest: resolver.Manifest, needed: dict[str, resolver.Needed]
) -> list[Item]:
    """Every crate the workspace asks for, with the one that provides it."""
    found: dict[tuple[str, str, str], Item] = {}
    for n in needed.values():
        item = found.setdefault(
            (n.crate, n.status, n.version),
            Item(
                crate=n.crate,
                status=n.status,
                version=n.version,
                fedora_versions=n.fedora_versions,
            ),
        )
        item.reqs.extend(sorted(n.reqs))
        item.needed_by.extend(sorted(n.needed_by))
        if n.missing_features:
            item.notes.append(f"missing features: {', '.join(n.missing_features)}")
        if n.missing_optional:
            item.notes.append(f"optional deps not in Fedora: {', '.join(n.missing_optional)}")
        if n.missing_dev:
            item.notes.append(f"dev deps not in Fedora: {', '.join(n.missing_dev)}")
    member = {m.crate: m for m in manifest.members}
    for r in manifest.requirements:
        if r.source == "workspace":
            version = member[r.crate].version if r.crate in member else ""
            item = found.setdefault(
                (r.crate, "workspace", version),
                Item(crate=r.crate, status="workspace", version=version),
            )
            if r.req and r.req != "*":
                item.reqs.append(r.req)
            item.needed_by.append(r.required_by)
        elif r.source == "path":
            item = found.setdefault((r.crate, "path", ""), Item(crate=r.crate, status="path"))
            if r.req and r.req != "*":
                item.reqs.append(r.req)
            item.needed_by.append(r.required_by)
            item.notes.append(f"from {r.path}")
        elif r.source in ("git", "alt"):
            item = found.setdefault(
                (r.crate, r.source, ""), Item(crate=r.crate, status=r.source)
            )
            if r.req and r.req != "*":
                item.reqs.append(r.req)
            item.needed_by.append(r.required_by)
            item.notes.append(
                f"from {r.origin}" if r.origin else "from a Git repository or alternate registry"
            )
    for r in manifest.optional_requirements():
        item = found.setdefault(
            (r.crate, "optional", ""), Item(crate=r.crate, status="optional")
        )
        item.reqs.append(r.req)
        item.needed_by.append(r.required_by)
        if r.enabled_by:
            item.notes.append(
                "asked for by the "
                + ", ".join(f"'{f}'" for f in r.enabled_by)
                + f" feature of {r.required_by}"
            )
    for item in found.values():
        item.reqs = sorted(set(item.reqs))
        item.needed_by = sorted(set(item.needed_by))
        if item.crate in manifest.dev_only():
            item.notes.append("only a member's tests need it")
    return sorted(found.values(), key=lambda i: (ORDER.index(i.status), i.crate, i.version))


def classify(
    manifest: resolver.Manifest, index: fedora.FedoraIndex, local: dict
) -> list[Item]:
    """Sort the workspace's crates into what Fedora has and what it lacks."""
    return items(
        manifest,
        resolver.resolve(
            manifest.seeds, index, local, report_provided=True
        ),
    )


def _row(item: Item) -> str:
    name = f"{item.crate} {item.version}".strip()
    return (
        f"{LABELS[item.status]:9} "
        + util.ljust_visible(name, 26)
        + util.ljust_visible(("req " + ", ".join(item.reqs)) if item.reqs else "", 24)
        + ("<- " + ", ".join(item.needed_by) if item.needed_by else "")
    )


def _relative(manifest: resolver.Manifest, path: Path) -> str:
    try:
        return str(path.relative_to(manifest.root))
    except ValueError:
        return str(path)


def report(manifest: resolver.Manifest, index: fedora.FedoraIndex, local: dict) -> None:
    """The workspace, its members, and where every crate it needs comes from."""
    found = classify(manifest, index, local)
    counts = collections.Counter(i.status for i in found)
    vendored = vendored_crates(manifest.root)

    if manifest.workspace:
        util.info(
            f"Workspace {manifest.root} ({len(manifest.members)} member crates, built from the source tree)"
        )
        for m in manifest.members:
            util.info(
                f"            {util.ljust_visible(f'{m.crate} {m.version}'.strip(), 26)}"
                + _relative(manifest, m.manifest)
            )
    else:
        only = manifest.members[0]
        util.info(f"Project {manifest.root}  ({only.crate} {only.version})")
    if not found:
        util.info("")
        util.info("Nothing outside its own sources.")
        return

    util.info("")
    for item in found:
        util.info(_row(item))
        for note in item.notes:
            util.info(f"            {note}")

    util.info("")
    util.info(
        f"{len(found)} requirements: "
        + ", ".join(f"{counts[s]} {SUMMARY[s]}" for s in ORDER if counts[s])
    )
    util.info("")
    for status in ORDER:
        if counts[status]:
            util.info(f"{LABELS[status]}: " + ADVICE[status])
    if counts["new"]:
        util.info("")
        util.info(
            "Package what Fedora lacks: rust-deps init --recursive "
            + " ".join(i.crate for i in found if i.status == "new")
            + "  (or keep those vendored)."
        )
    if counts["update"]:
        util.info(
            "Fedora ships another version of "
            + ", ".join(i.crate for i in found if i.status == "update")
            + ": update that package, or 'init --compat' beside it."
        )

    if vendored:
        replaced = replaced_source(manifest.root)
        util.info("")
        util.info(
            f"Vendored in {manifest.root / 'vendor'} "
            f"({len(vendored)} crates)"
            + (f", replacing the {replaced} source" if replaced else "")
            + ":"
        )
        # dropping a vendored crate only helps when the build actually reads
        # vendor/ (a replace-with source) and a Fedora package satisfies what
        # the workspace asks for; otherwise Cargo would be left without it
        satisfies = {i.crate for i in found if i.status == "fedora"}
        asked = {
            i.crate: i
            for i in found
            if i.status not in ("workspace", "path", "git", "alt")
        }
        drop = 0
        for name, version in vendored:
            have = index.versions(name)
            label = f"{name} {version}".strip()
            if name in {m.crate for m in manifest.members}:
                drop += 1
                util.info(
                    f"DROP      {util.ljust_visible(label, 26)}it is a workspace member; vendor/ holds a copy of it"
                )
            elif name in satisfies and replaced:
                drop += 1
                util.info(
                    f"DROP      {util.ljust_visible(label, 26)}Fedora has {', '.join(have[-3:])} and it satisfies the workspace requirement: remove it from vendor/ and build against crate({name})"
                )
            elif not replaced:
                util.info(
                    f"KEEP      {util.ljust_visible(label, 26)}vendor/ is not configured as a replacement source (.cargo/config.toml has no replace-with): the build does not read this copy"
                )
            elif name in asked:
                item = asked[name]
                if item.status == "update":
                    why = f"Fedora has {', '.join(have[-3:])}, which does not satisfy {', '.join(item.reqs)}: update that package, make a compat package, or keep it vendored"
                elif item.status == "features":
                    why = f"Fedora has {', '.join(have[-3:])} but not the features the workspace asks for: check its Cargo.toml patch or keep it vendored"
                elif item.status == "local":
                    why = "already packaged in the packages root: build against it or keep it vendored"
                else:
                    why = "not in Fedora: package it or keep it vendored"
                util.info(f"KEEP      {util.ljust_visible(label, 26)}{why}")
            elif have:
                util.info(
                    f"KEEP      {util.ljust_visible(label, 26)}Fedora has {', '.join(have[-3:])}, but nothing in the workspace asks for it: the vendored copy is unused"
                )
            else:
                util.info(
                    f"KEEP      {util.ljust_visible(label, 26)}not in Fedora: package it or keep it vendored"
                )
        util.info(
            f"{drop} of the {len(vendored)} vendored crates can be dropped; "
            "the rest are needed by the workspace or not covered by a Fedora package that satisfies it."
        )
    elif counts["new"]:
        util.info("")
        util.info(
            "No vendor/ directory: the build downloads these from crates.io. "
            "With the NEW ones packaged, the spec can build against Fedora's crates instead."
        )


def audit(manifest: resolver.Manifest, index: fedora.FedoraIndex, local: dict) -> dict:
    """The machine-readable answer: members, where each crate comes from, vendor audit."""
    return {
        "root": str(manifest.root),
        "manifest": str(manifest.path),
        "workspace": manifest.workspace,
        "members": [
            {"crate": m.crate, "version": m.version, "manifest": str(m.manifest)}
            for m in manifest.members
        ],
        "requirements": [i.__dict__ for i in classify(manifest, index, local)],
        "vendored": [
            {"crate": name, "version": version, "fedora": index.versions(name)}
            for name, version in vendored_crates(manifest.root)
        ],
    }


def cmd_workspace(args) -> None:
    index = fedora.fedora_index(args.refresh, args.target)
    local = packages.local_packages(args.root)
    for extra in args.local_root or []:
        local = {**packages.local_packages(Path(extra)), **local}
    manifests = [resolver.read_manifest(Path(t)) for t in args.projects]
    if args.json:
        print(json.dumps([audit(m, index, local) for m in manifests], indent=2))
        return
    for manifest in manifests:
        report(manifest, index, local)
