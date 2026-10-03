"""rust-deps update: bump a packaged crate to a newer version."""

from __future__ import annotations

from pathlib import Path
import tempfile
import tomllib

from . import archives
from . import build
from . import config
from . import cratesio
from . import edits
from . import fedora
from . import packages
from . import pkginit
from . import regen
from . import trial
from . import util
from . import versions


def update_version(
    pkg: packages.LocalPackage, wanted: str | None
) -> tuple[str | None, str | None]:
    """(version to update to, or None; why not). A compat package stays in its series."""
    if not pkg.version:
        return None, f"no spec yet; 'rust-deps regen {pkg.crate}' generates one"
    series = f"^{pkg.version}" if pkg.compat else "*"
    if wanted:
        if not any(v["num"] == wanted for v in cratesio.crate_versions(pkg.crate)):
            return None, f"{wanted} is not on crates.io (or yanked)"
        if pkg.compat and not versions.req_matches(series, wanted):
            return None, (
                f"{wanted} is outside the compat package's series ({series}); "
                f"'regen --version {wanted} --no-compat' makes it a regular package"
            )
    else:
        v = cratesio.pick_version(pkg.crate, series)
        wanted = v and v["num"]
    if not wanted or wanted == pkg.version:
        return None, f"already at {pkg.version}" + (
            "" if wanted else ", the newest release"
        ) + (f" matching {series}" if pkg.compat and not wanted else "")
    if (
        (old := versions.parse_version(pkg.version))
        and (new := versions.parse_version(wanted))
        and new < old
    ):
        return (
            None,
            f"{wanted} is older than {pkg.version}; 'regen --version {wanted}' downgrades",
        )
    return wanted, None


def repin_upstream_sources(
    pkg: packages.LocalPackage, old_sha: str | None, new_sha: str | None
) -> list[str]:
    """Point the extra sources taken from upstream git at the new release's commit.

    Returns the moved URLs that do not exist at the new commit.
    """
    text = pkg.config_file.read_text()
    if not old_sha or old_sha not in text:
        return []
    if not new_sha:
        util.action(
            f"{pkg.crate}: rust2rpm.toml takes sources from upstream commit {old_sha[:12]}, and the new "
            "release has no VCS info: point them at the release's commit by hand"
        )
        return []
    if old_sha == new_sha:
        return []
    pkg.config_file.write_text(text.replace(old_sha, new_sha))
    util.info(
        f"   rust2rpm.toml: upstream sources moved from commit {old_sha[:12]} to {new_sha[:12]}"
    )
    sources = (
        tomllib.loads(pkg.config_file.read_text())
        .get("package", {})
        .get("extra-sources", [])
    )
    return [
        s["file"]
        for s in sources
        if new_sha in s.get("file", "") and not pkginit.url_exists(s["file"])
    ]


def edits_drift(
    toml: dict, edits: dict, suggested: dict
) -> tuple[list[str], list[str]]:
    """(edits the new Cargo.toml needs that cargo-toml-edits.toml lacks, entries that name nothing in it)."""
    deps = {d["name"] for d in archives.manifest_deps(toml)}
    present = {
        "drop-dev-dependencies": deps,
        "drop-dependencies": deps,
        "drop-features": set(toml.get("features", {})),
    }
    kept = {
        *edits.get("set-version", {}),
        *edits.get("set-dev-version", {}),
        *edits.get("add-dev-dependencies", {}),
    }
    new = [
        f"{k}: {n}"
        for k in present
        for n in suggested.get(k, [])
        if n not in edits.get(k, []) and n not in kept
    ]
    stale = [
        f"{k}: {n}"
        for k, names in present.items()
        for n in edits.get(k, [])
        if n not in names
    ]
    return new, stale


def broken_dependents(updated: dict[str, str], root: Path) -> list[str]:
    """Local packages (not updated) whose crate requires an updated crate at a version it no longer has."""
    out = []
    for p in packages.local_packages(root).values():
        if p.crate in updated or not p.crate_file or not p.crate_file.exists():
            continue
        text = archives.read_crate_member(p.crate_file, "Cargo.toml")
        for d in archives.manifest_deps(
            tomllib.loads(edits.apply_edits(text, edits.load_edits(p.edits_file)))
        ):
            v = updated.get(d["crate_id"])
            if (
                v
                and not versions.is_foreign(d["target"])
                and not versions.req_matches(d["req"], v)
            ):
                out.append(
                    f"{p.crate} {p.version} needs {d['crate_id']} {d['req']} ({d['kind']})"
                )
    return sorted(set(out))


def cmd_update(args) -> None:
    pkgs = packages.select(args.root, args.crates, args.all)
    cratesio.forget_crate_versions(p.crate for p in pkgs)
    plan = []
    for pkg in pkgs:
        version, why = update_version(pkg, args.version)
        if version:
            plan.append((pkg, version))
        elif args.version or args.crates:
            util.info(f"== {pkg.crate}: {why}")
    if not plan:
        util.info("nothing to update")
        return
    order = [
        p.crate
        for stage in build.build_stages(
            [p for p, _ in plan], args.root, warn_outside=False
        )
        for p in stage
    ]
    plan.sort(key=lambda item: order.index(item[0].crate))
    util.info(
        "Updates, in build order: "
        + ", ".join(f"{p.crate} {p.version} -> {v}" for p, v in plan)
    )
    if args.dry_run:
        return
    index = fedora.fedora_index(args.refresh, args.target)
    updated, failed = {}, []
    for pkg, version in plan:
        util.info(f"== {pkg.crate} {pkg.version} -> {version}")
        with tempfile.TemporaryDirectory() as tmp:
            old_crate = (
                pkg.crate_file
                if pkg.crate_file.exists()
                else cratesio.download_crate(pkg.crate, pkg.version, Path(tmp))
            )
            old_sha = pkginit.vcs_commit(old_crate)
        new_crate = cratesio.download_crate(pkg.crate, version, pkg.dir)
        missing = repin_upstream_sources(pkg, old_sha, pkginit.vcs_commit(new_crate))
        for url in missing:
            util.action(
                f"{pkg.crate}: {url} does not exist at the new commit; fix it in rust2rpm.toml"
            )
        if pkginit.ships_license(new_crate) and tomllib.loads(
            pkg.config_file.read_text()
        ).get("package", {}).get("extra-sources"):
            util.action(
                f"{pkg.crate}: {version} ships its license text; drop the license extra-sources, "
                "license-files and [scripts.prep] copy from rust2rpm.toml if they were only for that"
            )
        if missing or not regen.regen(pkg, version):
            failed.append(pkg.crate)
            continue
        pkg = packages.local_packages(args.root)[pkg.crate]
        updated[pkg.crate] = version
        toml = tomllib.loads(archives.read_crate_member(pkg.crate_file, "Cargo.toml"))
        suggested, notes = pkginit.suggest_edits(
            toml, index, packages.local_packages(args.root)
        )
        new, stale = edits_drift(toml, edits.load_edits(pkg.edits_file), suggested)
        if new:
            util.action(
                f"{pkg.crate}: {version} needs edits missing from {config.EDITS_FILE}: {'; '.join(new)}"
            )
        if stale:
            util.action(
                f"{pkg.crate}: {config.EDITS_FILE} names what {version} no longer has: {'; '.join(stale)}; "
                "remove them, and their comments in rust2rpm.toml"
            )
        for n in notes:
            if n.startswith("required dependencies are not packaged"):
                util.action(f"{pkg.crate}: {n}")
        if not args.no_trial and not trial.trial(pkg, discover=False):
            util.action(
                f"{pkg.crate}: the tests fail at {version}; 'rust-deps trial --discover {pkg.crate}' "
                "suggests a new [tests] table"
            )
            failed.append(pkg.crate)
    for line in broken_dependents(updated, args.root):
        util.action(
            f"{line}: it does not accept the update; update it too, or patch its requirement"
        )
    if updated:
        names = " ".join(updated)
        util.info(f"\nUpdated: {', '.join(f'{c} {v}' for c, v in updated.items())}")
        util.info(
            f"Next: rust-deps srpm {names}; rust-deps check-targets {names} -r <chroot>; then copr or mock-chain"
        )
        if adopted := [
            c
            for c in updated
            if packages.local_packages(args.root)[c].dist_git is not None
        ]:
            util.info(
                f"In Fedora: 'rust-deps dist-git {' '.join(adopted)}' prepares the dist-git updates"
            )
    if failed:
        util.die(f"update incomplete for: {', '.join(dict.fromkeys(failed))}")
