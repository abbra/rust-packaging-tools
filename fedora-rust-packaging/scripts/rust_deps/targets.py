"""rust-deps target check and COPR status reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import collections
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import archives
from . import build
from . import config
from . import copr
from . import coprlogs
from . import fedora
from . import packages
from . import reviewstatus
from . import util
from . import versions


@dataclass
class TargetProblem:
    req: copr.MissingReq
    why: str
    external: bool  # missing from the target and not in the tree (vs. a local package's problem)


def check_target(
    pkg: packages.LocalPackage,
    chroot: str,
    local: dict[str, packages.LocalPackage],
    refresh: bool = False,
) -> tuple[list[TargetProblem], list[str]]:
    """Would the package's BuildRequires resolve in this chroot's release?

    Local packages built for the target count, if their version and features
    fit.  Returns the problems and informational notes.
    """
    rel = (
        fedora.chroot_release(chroot)
        if re.search(r"-(\d+|rawhide)-[^-]+$", chroot)
        else chroot
    )
    toml, brs = copr.build_info(pkg)
    kinds = copr.dep_kinds(pkg)
    reqs: dict[tuple, copr.MissingReq] = {}
    for line in brs:
        if copr.CRATE_REQ_RE.search(line):
            req = copr.MissingReq.parse(line)
            have = reqs.setdefault(req.key(), req)
            have.features += [f for f in req.features if f not in have.features]
    idx = copr.target_index(chroot, refresh)
    problems, notes = [], []
    for req in reqs.values():
        k = kinds.get(req.crate, set())
        req.tests_only = bool(k) and k <= {"dev"}
        dep = local.get(req.crate)
        if (
            dep
            and dep.version
            and dep.builds_in(chroot)
            and versions.req_matches(req.req, dep.version)
        ):
            lacking = [
                f
                for f in req.features
                if f not in copr.provided_features(copr.build_info(dep)[0])
            ]
            if lacking:
                problems.append(
                    TargetProblem(
                        req,
                        f"local {dep.crate} {dep.version} lacks feature(s) "
                        f"{', '.join(lacking)} (dropped in its {config.EDITS_FILE}?)",
                        False,
                    )
                )
            continue
        version, why = copr.target_availability(idx, rel, req)
        if version:
            continue
        if dep and not dep.builds_in(chroot):
            why += f"; the local package is {dep.scope}"
        elif dep:
            why += f"; the local package is {dep.version or 'not generated'}"
        problems.append(TargetProblem(req, why, True))
    # each feature subpackage requires what its feature refers to: crate(dep/feat)
    deps = {
        d["name"]: d
        for d in archives.manifest_deps(toml)
        if d["kind"] == "normal" and not versions.is_foreign(d["target"])
    }
    for feat, items in sorted(toml.get("features", {}).items()):
        for item in items:
            if "/" not in item or item.startswith("dep:"):
                continue
            name, dfeat = item.split("/", 1)
            d = deps.get(name.rstrip("?"))
            if not d:
                continue
            req = copr.MissingReq(item, d["crate_id"], [dfeat], d["req"])
            dep = local.get(d["crate_id"])
            if (
                dep
                and dep.version
                and dep.builds_in(chroot)
                and versions.req_matches(d["req"], dep.version)
            ):
                if dfeat not in copr.provided_features(copr.build_info(dep)[0]):
                    problems.append(
                        TargetProblem(
                            req,
                            f"feature '{feat}' needs it, but the local {dep.crate} lacks "
                            f"feature {dfeat} (dropped?): +{feat}-devel could not be installed; "
                            f"drop '{feat}' as well",
                            False,
                        )
                    )
                continue
            version, why = copr.target_availability(idx, rel, req)
            if (
                not version
                and copr.target_availability(
                    idx, rel, copr.MissingReq(item, d["crate_id"], [""], d["req"])
                )[0]
            ):
                problems.append(
                    TargetProblem(
                        req,
                        f"feature '{feat}' needs it: {why}; +{feat}-devel could not be "
                        "installed",
                        True,
                    )
                )
    if (
        not pkg.compat
        and idx.versions(pkg.crate)
        and pkg.version not in idx.versions(pkg.crate)
    ):
        notes.append(f"{rel} ({', '.join(idx.versions(pkg.crate))})")
    return problems, notes


def report_target_check(
    plan: list[tuple[packages.LocalPackage, list[str]]],
    local: dict[str, packages.LocalPackage],
    refresh: bool = False,
    quiet: bool = False,
) -> int:
    """Check (package, chroots) pairs by release and print the result; returns the number of problems."""
    count = 0
    external: dict[str, dict[str, tuple[str, set[str]]]] = collections.defaultdict(dict)
    for pkg, chroots in plan:
        if not pkg.version:
            continue
        releases = sorted({fedora.chroot_release(c) for c in chroots})
        results = {
            rel: check_target(pkg, f"{rel}-x86_64", local, refresh) for rel in releases
        }
        if quiet and not any(r[0] for r in results.values()):
            continue
        util.info(f"{pkg.crate} {pkg.version}:")
        groups: dict[tuple, list[str]] = {}
        shadows = [n for rel in releases for n in results[rel][1]]
        for rel in releases:
            probs = results[rel][0]
            lines = tuple(
                f"missing {pr.req.label()}{' (tests only)' if pr.req.tests_only else ''}: {pr.why}"
                for pr in probs
            )
            groups.setdefault(lines, []).append(rel)
            count += len(probs)
            for pr in probs:
                if pr.external:
                    _, users = external[rel].setdefault(pr.req.label(), (pr.why, set()))
                    users.add(pkg.crate + (" (tests)" if pr.req.tests_only else ""))
        for lines, rels in groups.items():
            if not lines:
                if not quiet:
                    util.info(f"   ok       {' '.join(rels)}")
                continue
            util.info(f"   MISSING  {' '.join(rels)}")
            for ln in lines:
                util.info(f"            {ln}")
        if shadows and not quiet:
            util.info(
                f"   note: {', '.join(shadows)} ship other versions of rust-{pkg.crate}, with the same package "
                "name: a build that needs both cannot install them together. If one does, make this a "
                f"compat package: regen --compat {pkg.crate}"
            )
    if external:
        util.info("missing from a target and not in this tree:")
        for rel, reqs in sorted(external.items()):
            util.info(f"    {rel}:")
            for lbl, (why, users) in sorted(reqs.items()):
                util.info(f"      {lbl}  ({why}; needed by {', '.join(sorted(users))})")
        util.info(
            "  package them for that target ('resolve -r <chroot>', 'init --recursive -r <chroot>'), "
            "or leave the target out for the packages that need them"
        )
    return count


def project_chroots(project: str) -> list[str]:
    owner, _, name = project.partition("/")
    try:
        return sorted(
            copr.copr_api("project/", ownername=owner, projectname=name)["chroot_repos"]
        )
    except urllib.error.HTTPError as exc:
        util.die(f"cannot read COPR project {project}: {exc}")


def cmd_check_targets(args) -> None:
    if not args.chroot and not args.project:
        util.die(
            "name the targets with -r CHROOT, or --project to use all chroots of a COPR project"
        )
    chroots = args.chroot or project_chroots(args.project)
    pkgs = [
        p
        for s in build.build_stages(
            packages.select(args.root, args.crates, args.all),
            args.root,
            warn_outside=False,
        )
        for p in s
    ]
    plan = [(p, [c for c in chroots if p.builds_in(c)]) for p in pkgs]
    n = report_target_check(
        [(p, cs) for p, cs in plan if cs],
        packages.local_packages(args.root),
        args.refresh,
        quiet=args.quiet,
    )
    if n:
        util.die(
            f"{n} requirement(s) would not resolve (BuildRequires, or what a feature subpackage requires)"
        )
    util.info(
        f"all BuildRequires resolve in {', '.join(sorted({fedora.chroot_release(c) for c in chroots}))}"
    )


def copr_retry_plan(
    stages: list[list[packages.LocalPackage]], root: Path, project: str, only: list[str]
) -> list[list[tuple[packages.LocalPackage, list[str]]]]:
    """Per stage, the packages and chroots worth resubmitting (see CoprState)."""
    state = copr.CoprState(project, root)
    selected = {p.crate for s in stages for p in s}
    plan = []
    for s in stages:
        out = []
        for p in s:
            d = state.diagnose(p)
            if not d.build:
                util.info(f"   {p.crate}: no COPR build of {d.evr} yet; submitting it")
                out.append((p, only))
                continue
            retry = [
                c
                for c, cd in sorted(d.chroots.items())
                if cd.verdict == "retry" and (not only or c in only)
            ]
            for c in retry:
                for dep in sorted(d.chroots[c].via - selected):
                    util.action(
                        f"{p.crate}: {c} needs {dep} resubmitted as well; add it to the command"
                    )
            left: dict[str, list[str]] = collections.defaultdict(list)
            for c, cd in sorted(d.chroots.items()):
                if cd.verdict not in ("ok", "retry") and (not only or c in only):
                    left[
                        (
                            "still building"
                            if cd.verdict in ("active", "wait")
                            else cd.verdict
                        )
                    ].append(c)
            if left:
                util.info(
                    f"   {p.crate}: not resubmitting "
                    + "; ".join(f"{why}: {' '.join(cs)}" for why, cs in left.items())
                    + " (see 'copr-status')"
                )
            if retry:
                out.append((p, retry))
        plan.append(out)
    return plan


def group_chroots(
    diags: dict[str, copr.ChrootDiag],
) -> list[tuple[list[str], copr.ChrootDiag]]:
    """Chroots with the same verdict and notes, together."""
    groups: dict[tuple, list[copr.ChrootDiag]] = {}
    for c in sorted(diags):
        cd = diags[c]
        groups.setdefault((cd.verdict, tuple(cd.notes), cd.build), []).append(cd)
    return [([cd.chroot for cd in g], g[0]) for g in groups.values()]


def cmd_copr_status(args) -> None:
    if args.json:
        util._HYPERLINKS = False
    stages = build.build_stages(
        packages.select(args.root, args.crates, args.all), args.root, warn_outside=False
    )
    while True:
        state = copr.CoprState(args.project, args.root, args.refresh)
        diags = [[state.diagnose(p) for p in s] for s in stages]
        active = sum(
            1
            for s in diags
            for pd in s
            for c, cd in pd.chroots.items()
            if cd.verdict in ("active", "wait")
            and (not args.chroot or c in args.chroot)
        )
        if not args.wait or not active:
            break
        util.info(
            f"{time.strftime('%H:%M')} {active} chroot(s) still building or waiting; "
            f"checking again in {args.interval}s"
        )
        time.sleep(args.interval)
    for pd in (pd for s in diags for pd in s):  # restrict to the chroots asked for
        pd.chroots = {
            c: cd for c, cd in pd.chroots.items() if not args.chroot or c in args.chroot
        }
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "crate": pd.pkg.crate,
                        "evr": pd.evr,
                        "stage": i,
                        "build": pd.build["id"] if pd.build else None,
                        "chroots": {
                            c: {
                                "build": cd.build,
                                "state": cd.state,
                                "verdict": cd.verdict,
                                "notes": cd.notes,
                                "log": str(cd.log) if cd.log else None,
                                "log_url": cd.log_url,
                            }
                            for c, cd in pd.chroots.items()
                        },
                    }
                    for i, s in enumerate(diags, 1)
                    for pd in s
                ],
                indent=2,
            )
        )
        return
    label = {
        "ok": "ok",
        "active": "BUILDING",
        "wait": "WAIT",
        "retry": "RETRY",
        "blocked": "BLOCKED",
        "error": "FAILED",
    }
    for i, s in enumerate(diags, 1):
        util.info(f"=== stage {i}")
        for pd in s:
            if not pd.build:
                util.info(f"{pd.pkg.crate} {pd.evr}: no COPR build of this version")
                continue
            util.info(
                f"{pd.pkg.crate} {pd.evr}: build {util.link(reviewstatus.copr_build_url(pd.build['id']), str(pd.build['id']))}"
            )
            for chroots, cd in group_chroots(pd.chroots):
                older = (
                    f"  (build {util.link(reviewstatus.copr_build_url(cd.build), str(cd.build))})"
                    if cd.build != pd.build["id"]
                    else ""
                )
                util.info(f"   {label[cd.verdict]:8} {' '.join(chroots)}{older}")
                for n in cd.notes:
                    util.info(f"            {n}")
                if cd.log:
                    util.info(
                        f"            log: {cd.log}"
                        + (
                            f" ({len(chroots)} chroots; this is {cd.chroot})"
                            if len(chroots) > 1
                            else ""
                        )
                    )
    all_pd = [pd for s in diags for pd in s]
    verdicts = collections.Counter(
        cd.verdict for pd in all_pd for cd in pd.chroots.values()
    )
    # every package looked at, including dependencies outside the selection
    retry = [
        pd.pkg.crate
        for pd in state.diagnosed().values()
        if not pd.build or any(cd.verdict == "retry" for cd in pd.chroots.values())
    ]
    util.info("")
    util.info(
        "summary: "
        + (
            ", ".join(
                f"{n} {label[v].lower()}"
                for v, n in sorted(
                    verdicts.items(), key=lambda kv: copr.VERDICT_RANK[kv[0]]
                )
            )
            or "no builds"
        )
    )
    if verdicts["active"] or verdicts["wait"]:
        util.info(
            "- builds are still running: run 'copr-status' again when they have finished"
        )
    if retry:
        me = f"rust-deps --root {args.root}"
        only = "".join(f" -r {c}" for c in args.chroot)
        util.info(
            f"- resubmit what can succeed now:\n    {me} copr --retry-failed --project {args.project}{only} "
            + " ".join(retry)
        )
    external: dict[str, dict[str, tuple[str, set[str]]]] = collections.defaultdict(dict)
    tests_only = set()
    for pd in all_pd:
        for cd in pd.chroots.values():
            for req, why in cd.external:
                _, users = external[fedora.chroot_release(cd.chroot)].setdefault(
                    req.label(), (why, set())
                )
                users.add(pd.pkg.crate + (" (tests)" if req.tests_only else ""))
                if req.tests_only:
                    tests_only.add(fedora.chroot_release(cd.chroot))
    if external:
        util.info(
            "- missing from a target and not in this tree (decide per target: package them there "
            "with 'resolve -r <chroot>' and 'init -r <chroot>', or leave the target out):"
        )
        for rel, reqs in sorted(external.items()):
            util.info(f"    {rel}:")
            for lbl, (why, users) in sorted(reqs.items()):
                util.info(f"      {lbl}  ({why}; needed by {', '.join(sorted(users))})")
        if tests_only:
            util.info(
                "  '(tests)': only the tests need it; building that chroot without them is possible for "
                "the whole project: copr-cli edit-chroot --rpmbuild-without check "
                f"{args.project}/<chroot> (targets: {', '.join(sorted(tests_only))})"
            )
    host_arch = (
        os.uname().machine
    )  # reproduce on the host's architecture when it failed there too
    errors = [
        (pd, c)
        for pd in all_pd
        for c, cd in sorted(
            pd.chroots.items(),
            key=lambda kv: (not kv[0].endswith("-" + host_arch), kv[0]),
        )
        if cd.verdict == "error"
    ]
    if errors:
        me = f"rust-deps --root {args.root}"
        util.info(
            "- build errors: read the summary ('copr-log'), reproduce in mock, fix, 'srpm', then submit "
            "the package and its dependents with 'copr -r <chroot>':"
        )
        seen = set()
        for pd, c in errors:
            key = (pd.pkg.crate, copr.mock_chroot(c).rsplit("-", 1)[0])
            if key in seen:
                continue
            seen.add(key)
            util.info(
                f"    {me} copr-log {pd.pkg.crate} -r {c} --project {args.project}"
            )
            util.info(
                f"    {me} mock-chain -r {copr.mock_chroot(c)} {' '.join(coprlogs.local_closure(pd.pkg, c, state.local))}"
            )
