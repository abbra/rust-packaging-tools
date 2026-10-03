"""rust-deps review-plan: the read-only submission plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import collections
import json
import urllib.error
import urllib.parse
import urllib.request

from . import build
from . import distgit
from . import fedora
from . import packages
from . import reviewrequest
from . import reviewstatus
from . import util


@dataclass
class PlanItem:
    pkg: packages.LocalPackage
    kind: str  # review | update | skip
    state: str  # filed | ready | not-ready | (for update/skip: "")
    detail: str  # bug link, draft path, what is missing, why skipped
    after: list[str]  # RPM names this one waits for (their tickets, or their updates)


def dist_git_has(rpm_name: str) -> bool:
    try:
        reviewrequest._get(f"https://src.fedoraproject.org/api/0/rpms/{rpm_name}")
        return True
    except urllib.error.HTTPError:
        return False


def review_plan(
    pkgs: list[packages.LocalPackage], root: Path, project: str | None, chroot: str
) -> list[list[PlanItem]]:
    """The packages by build stage, each with how it gets into Fedora and whether it is ready."""
    stages = build.build_stages(pkgs, root, warn_outside=False)
    local = packages.local_packages(root)
    deps = build.local_deps([p for s in stages for p in s], root)
    rawhide = fedora.fedora_index(False, "fedora-rawhide-x86_64")
    plan, kinds = [], {}
    for stage in stages:
        items = []
        for pkg in stage:
            have = ", ".join(rawhide.versions(pkg.crate)) or "no build yet"
            if (dg := pkg.dist_git) is not None:
                how = (
                    "marked from" if dg.get("packaging") == "local" else "adopted from"
                )  # adopt --mark-only
                source = f"{how} dist-git {dg.get('branch')} {str(dg.get('commit', ''))[:10]}"
                if pkg.version in rawhide.versions(pkg.crate):
                    item = PlanItem(
                        pkg, "skip", "", f"in Fedora ({source}); nothing to submit", []
                    )
                elif dg.get("packaging") == "local":
                    # the local packaging is for the targets that lack the crate, not a proposal for Fedora
                    item = PlanItem(
                        pkg,
                        "skip",
                        "",
                        f"in Fedora (Rawhide: {have}; {source}): packaged here only "
                        f"for targets that lack {pkg.version}; to update Fedora, 'adopt {pkg.crate}' "
                        "(without --mark-only), then 'dist-git'",
                        [],
                    )
                else:
                    item = PlanItem(
                        pkg,
                        "update",
                        "",
                        f"in Fedora (Rawhide: {have}; {source}): a pull request "
                        f"to its dist-git with {pkg.version}",
                        [],
                    )
                    upd = pkg.dir / distgit.UPDATE_STATE
                    if (
                        upd.exists()
                        and (st := json.loads(upd.read_text())).get("version")
                        == pkg.version
                    ):
                        item.detail += (
                            f"; pushed: {st.get('pr_url')}"
                            if st.get("pushed")
                            else f"; prepared on {st['branch']} in {st['checkout']} ('dist-git --push')"
                        )
            elif pkg.targets is not None:
                item = PlanItem(
                    pkg,
                    "skip",
                    "",
                    f"built only for {', '.join(pkg.targets)} (targets.toml); "
                    "Rawhide has the crate, so there is nothing to review",
                    [],
                )
            elif dist_git_has(pkg.rpm_name):
                item = PlanItem(
                    pkg,
                    "update",
                    "",
                    f"Fedora has {pkg.rpm_name} (Rawhide: {have}): adopt its "
                    f"packaging ('adopt {pkg.crate}'), then a pull request to its dist-git "
                    "instead of a review",
                    [],
                )
            else:
                item = PlanItem(pkg, "review", "", "", [])
                bug = reviewrequest.request_state(pkg).get("bug")
                if bug:
                    item.state, item.detail = "filed", reviewstatus.bug_url(bug)
                else:
                    try:
                        build.srpm_of(pkg)
                        problem = reviewrequest.local_review_problem(pkg)
                    except SystemExit:
                        problem = "no SRPM of the current version; run 'srpm'"
                    if not problem and project:
                        problem = reviewrequest.copr_review_urls(pkg, project, chroot)[
                            1
                        ]
                    draft = pkg.dir / reviewrequest.REQUEST_DRAFT
                    if problem:
                        item.state, item.detail = "not-ready", problem
                    elif (
                        not draft.exists()
                        or draft.stat().st_mtime < pkg.spec.stat().st_mtime
                    ):
                        item.state, item.detail = (
                            "not-ready",
                            "no current draft; run 'review-request' (without --file)",
                        )
                    else:
                        item.state, item.detail = "ready", str(draft)
            kinds[pkg.crate] = item.kind
            items.append(item)
        plan.append(items)
    for item in (i for s in plan for i in s):
        if item.kind != "skip":
            item.after = sorted(
                local[d].rpm_name
                for d in deps.get(item.pkg.crate, ())
                if kinds.get(d) in ("review", "update")
            )
    return plan


def cmd_review_plan(args) -> None:
    plan = review_plan(
        packages.select(args.root, args.crates, args.all),
        args.root,
        args.project,
        args.chroot,
    )
    items = [i for s in plan for i in s]
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "stage": n,
                        "package": i.pkg.rpm_name,
                        "crate": i.pkg.crate,
                        "version": i.pkg.version,
                        "kind": i.kind,
                        "state": i.state,
                        "detail": i.detail,
                        "after": i.after,
                    }
                    for n, s in enumerate(plan, 1)
                    for i in s
                ],
                indent=2,
            )
        )
        return
    label = {"filed": "FILED", "ready": "READY", "not-ready": "NOT READY"}
    step = 0
    util.info(
        "Submit in this order: a ticket names the tickets of the packages it waits for (Depends On), "
        "and reviews are imported and built in the same order."
    )
    for n, stage in enumerate(plan, 1):
        todo = [i for i in stage if i.kind != "skip"]
        if not todo:
            continue
        util.info(f"\n=== stage {n}")
        for i in todo:
            step += 1
            what = "update" if i.kind == "update" else label[i.state]
            util.info(f"{step:3}. {i.pkg.rpm_name} {i.pkg.version}  {what}")
            util.info(f"       {i.detail}")
            if i.after:
                util.info(f"       after: {', '.join(i.after)}")
    skipped = [i for i in items if i.kind == "skip"]
    if skipped:
        util.info("\nno review needed:")
        for i in skipped:
            util.info(f"     {i.pkg.rpm_name} {i.pkg.version}: {i.detail}")
    others = sorted(
        d.name
        for d in args.root.iterdir()
        if d.is_dir() and not (d / "rust2rpm.toml").exists() and any(d.glob("*.spec"))
    )
    if others and args.all:
        util.info("\nother specs in the root (not managed by rust-deps):")
        for d in others:
            for name in (f.stem for f in sorted((args.root / d).glob("*.spec"))):
                how = (
                    "Fedora has it: update its dist-git repository"
                    if dist_git_has(name)
                    else "new: a review by hand, after the Rust packages it builds against"
                )
                util.info(f"     {name}: {how} ({args.root / d})")
    counts = collections.Counter(i.state or i.kind for i in items)
    util.info("\nsummary: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    if counts["ready"]:
        util.info(
            f"file the READY ones, in this order: rust-deps review-request --project {args.project or 'P'} "
            "--fas NAME --file "
            + " ".join(i.pkg.crate for i in items if i.state == "ready")
        )
