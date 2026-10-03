"""rust-deps build order, mock chains and COPR submission."""

from __future__ import annotations

from pathlib import Path
import json
import re
import shutil
import sys

from . import config
from . import copr
from . import packages
from . import reviewstatus
from . import targets
from . import util


def build_deps(pkg: packages.LocalPackage) -> set[str]:
    """Crates the package's build needs: its BuildRequires as rust2rpm generates them.

    Dev-dependencies count only when %check runs, so a crate whose tests are
    off does not need its own dependents (e.g. synta's dev-dependency on
    synta-certificate, which depends on synta).
    """
    return {
        m.group(1)
        for line in copr.build_info(pkg)[1]
        if (m := copr.CRATE_REQ_RE.search(line))
    } - {pkg.crate}


def local_deps(pkgs: list[packages.LocalPackage], root: Path) -> dict[str, set[str]]:
    """crate -> local packages (under root) that its build needs."""
    local = packages.local_packages(root)
    return {p.crate: build_deps(p) & set(local) for p in pkgs}


def build_stages(
    pkgs: list[packages.LocalPackage], root: Path, warn_outside: bool = True
) -> list[list[packages.LocalPackage]]:
    """Group packages into stages; each stage only needs earlier stages (and Fedora)."""
    names = {p.crate for p in pkgs}
    deps = local_deps(pkgs, root)
    for c, ds in deps.items():
        outside = ds - names
        if outside and warn_outside:
            util.warn(
                f"{c} also needs local packages not in this build: {', '.join(sorted(outside))}"
            )
        deps[c] = ds & names
    stages, done = [], set()
    while len(done) < len(pkgs):
        stage = [p for p in pkgs if p.crate not in done and deps[p.crate] <= done]
        if not stage:
            left = sorted(names - done)
            util.die(
                "dependency cycle between: "
                + ", ".join(left)
                + ". Their BuildRequires need each other; "
                "usually through a dev-dependency: turn off the tests that need it ([tests] in "
                "rust2rpm.toml) or drop that dev-dependency, then regen"
            )
        stages.append(stage)
        done |= {p.crate for p in stage}
    return stages


def cmd_order(args) -> None:
    stages = build_stages(packages.select(args.root, args.crates, args.all), args.root)
    if args.json:
        print(json.dumps([[p.crate for p in s] for s in stages], indent=2))
        return
    for i, s in enumerate(stages, 1):
        util.info(f"stage {i}: {' '.join(p.crate for p in s)}")


def srpm_of(pkg: packages.LocalPackage) -> Path:
    found = sorted(pkg.dir.glob(f"{pkg.rpm_name}-{pkg.version}-*.src.rpm"))
    if not found:
        util.die(
            f"{pkg.crate}: no SRPM for {pkg.version}; run 'rust-deps srpm {pkg.crate}'"
        )
    return found[-1]


def cmd_mock_chain(args) -> None:
    stages = build_stages(packages.select(args.root, args.crates, args.all), args.root)
    srpms = [str(srpm_of(p)) for s in stages for p in s]
    repo = Path(args.localrepo or (config.CACHE_DIR / "mock-repo"))
    if not args.dry_run:
        # mock --chain skips a package whose NVR it built before; a changed spec keeps its NVR
        for s in srpms:
            for old in (repo / "results" / args.chroot).glob(
                Path(s).name.removesuffix(".src.rpm")
            ):
                util.info(f"   rebuilding {old.name}: removing the earlier result")
                shutil.rmtree(old)
    cmd = ["mock", "-r", args.chroot, "--chain", "--localrepo", str(repo), *srpms]
    util.info("$ " + " ".join(cmd))
    if args.dry_run:
        return
    sys.exit(util.run(cmd).returncode)


def copr_submit(
    plan: list[list[tuple[packages.LocalPackage, list[str]]]],
    project: str,
    dry_run: bool,
) -> list[str]:
    """Submit (package, chroots) stages as chained COPR batches; returns the build IDs.

    All builds of a stage share one batch (--with-build-id), and each batch
    waits for the previous one (--after-build-id).  A build that only names a
    batch with --after-build-id is not part of it: without --with-build-id
    the next stage would wait for the first build of a stage alone.  An
    empty chroot list means all chroots of the project.
    """
    after = None
    ids = []
    for i, stage in enumerate(plan, 1):
        if not stage:
            continue
        util.info(f"=== stage {i}")
        first = None
        for p, chroots in stage:
            chain = (
                ["--with-build-id", first]
                if first
                else (["--after-build-id", after] if after else [])
            )
            cmd = [
                "copr-cli",
                "build",
                "--nowait",
                *(a for c in chroots for a in ("-r", c)),
                *chain,
                project,
                str(srpm_of(p)),
            ]
            util.info("$ " + " ".join(cmd))
            if dry_run:
                bid = f"DRYRUN-{len(ids) + 1}"
            else:
                res = util.run(cmd, capture_output=True)
                if res.returncode:
                    util.die(res.stdout + res.stderr)
                m = re.findall(r"Created builds: (\d+)", res.stdout)
                if not m:
                    util.die(f"cannot parse build id:\n{res.stdout}")
                bid = m[-1]
            util.info(
                f"   {p.crate}: build {bid if dry_run else util.link(reviewstatus.copr_build_url(bid), bid)}"
            )
            if not dry_run:
                copr.record_copr_build(project, p.crate, int(bid))
            ids.append(bid)
            first = first or bid
        after = first
    return ids


def limit_to_targets(
    plan: list[list[tuple[packages.LocalPackage, list[str]]]], project: str
) -> list[list[tuple[packages.LocalPackage, list[str]]]]:
    """Drop the chroots a package is not built for (targets.toml, or adopted: where a target has it)."""
    if not any(p.limited for s in plan for p, _ in s):
        return plan
    owner, _, name = project.partition("/")
    all_chroots = sorted(
        copr.copr_api("project/", ownername=owner, projectname=name)["chroot_repos"]
    )
    out = []
    for s in plan:
        stage = []
        for p, chroots in s:
            if not p.limited:
                stage.append((p, chroots))
                continue
            keep = [c for c in (chroots or all_chroots) if p.builds_in(c)]
            if keep:
                stage.append((p, keep))
            else:
                util.info(f"   {p.crate}: not built in these chroots ({p.scope})")
        out.append(stage)
    return out


def cmd_copr(args) -> None:
    stages = build_stages(
        packages.select(args.root, args.crates, args.all),
        args.root,
        warn_outside=not args.retry_failed,
    )  # a retry needs only the failed ones
    if args.retry_failed:
        plan = targets.copr_retry_plan(stages, args.root, args.project, args.chroot)
        if not any(plan):
            util.info("nothing to resubmit")
            return
    else:
        plan = [[(p, args.chroot) for p in s] for s in stages]
    plan = limit_to_targets(plan, args.project)
    if not args.force:
        pairs = [
            (p, cs or targets.project_chroots(args.project))
            for s in plan
            for p, cs in s
        ]
        if n := targets.report_target_check(
            pairs, packages.local_packages(args.root), quiet=True
        ):
            util.die(
                f"{n} requirement(s) would not resolve in the chroots above; those builds would fail or their "
                "subpackages could not be installed. "
                "Fix them (see 'COPR build failures' in the manual), leave those chroots out with -r, "
                "or pass --force to submit anyway"
            )
    ids = copr_submit(plan, args.project, args.dry_run)
    if args.wait and not args.dry_run:
        util.info("waiting for the builds to finish")
        if util.run(["copr-cli", "watch-build", *ids]).returncode:
            util.die("COPR builds failed; run 'copr-status' to see why")
