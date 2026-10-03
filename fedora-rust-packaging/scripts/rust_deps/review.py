"""rust-deps review: run fedora-review for a package."""

from __future__ import annotations

from pathlib import Path
import json
import re
import shutil

from . import build
from . import config
from . import packages
from . import util

# fedora-review's default mock options plus --isolation=simple: under
# systemd-nspawn, mock prints a "Note: ..." line that fedora-review reads as
# part of evaluated macros (e.g. "Release: 1Note: ..."), and parsing the spec
# fails.  --uniqueext keeps the review root apart from the mock-chain one.
REVIEW_MOCK_OPTS = (
    "--no-cleanup-after --no-clean --plugin-option=tmpfs:keep_mounted=True "
    "--isolation=simple --uniqueext=rust-deps-review"
)


def expected_review_issue(issue: dict, pkg: packages.LocalPackage | None) -> str | None:
    """Why a failed fedora-review check is expected for a rust2rpm spec, or None.

    Without a local package, any single crate directory in the registry counts.
    """
    note = issue.get("note") or ""
    if issue.get("name") == "CheckFileDuplicates":
        dups = re.findall(r"File listed twice: (\S+)", note)
        if pkg:
            instdir = f"/usr/share/cargo/registry/{pkg.crate}-{pkg.version}/"
        else:
            m = re.match(r"/usr/share/cargo/registry/[^/]+/", dups[0]) if dups else None
            instdir = m.group(0) if m else "\0"
        if dups and all(d.startswith(instdir) for d in dups):
            return (
                "rust2rpm lists %doc/%license files inside %{crate_instdir}/, which %files also "
                "owns whole; standard for Fedora Rust packages"
            )
    return None


def review_one(
    pkg: packages.LocalPackage,
    chroot: str,
    results: Path,
    closure: set[str],
    local: dict,
) -> bool:
    work = config.CACHE_DIR / "review" / chroot / pkg.crate
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    shutil.copy2(pkg.spec, work)
    shutil.copy2(build.srpm_of(pkg), work)
    cmd = [
        "fedora-review",
        "-n",
        pkg.rpm_name,
        "-m",
        str(chroot),
        "-B",
        "-o",
        REVIEW_MOCK_OPTS,
    ]
    if closure:
        depdir = work / "deps"
        depdir.mkdir()
        for d in sorted(closure):
            built = sorted(results.glob(f"{local[d].rpm_name}-{local[d].version}-*"))
            rpms = [
                r
                for r in (built[-1].glob("*.rpm") if built else [])
                if not r.name.endswith(".src.rpm")
            ]
            if not rpms:
                util.action(
                    f"{pkg.crate}: no RPMs of local dependency {d} {local[d].version} in {results}; "
                    "build them first with 'mock-chain'"
                )
                return False
            for r in rpms:
                shutil.copy2(r, depdir)
        cmd += ["-L", str(depdir)]
    util.info(
        f"== review {pkg.crate} {pkg.version}"
        + (f" (local deps: {', '.join(sorted(closure))})" if closure else "")
    )
    # a clean root: fedora-review keeps it between runs (--no-clean), and
    # packages left from another review could hide a missing BuildRequires
    util.run(
        [
            "mock",
            "-r",
            str(chroot),
            "--isolation=simple",
            "--uniqueext=rust-deps-review",
            "--scrub=chroot",
        ],
        capture_output=True,
    )
    res = util.run(cmd, cwd=work, capture_output=True)
    (work / "fedora-review.out").write_text(res.stdout + res.stderr)
    report = work / f"review-{pkg.rpm_name}"
    if not (report / "review.json").exists():
        util.info(
            f"   FAILED  fedora-review did not produce a report (see {work / 'fedora-review.out'})"
        )
        for ln in (res.stdout + res.stderr).strip().splitlines()[-5:]:
            util.info(f"      {ln}")
        return False
    data = json.loads((report / "review.json").read_text())
    pending = sum(
        1
        for groups in data["results"].values()
        for items in groups.values()
        for it in items
        if it["result"] == "pending"
    )
    unexplained = []
    for issue in data["issues"]:
        why = expected_review_issue(issue, pkg)
        text = issue["text"] + (f" ({issue['note']})" if issue.get("note") else "")
        if why:
            util.info(f"   [~] {text}\n       expected: {why}")
        else:
            unexplained.append(text)
            util.info(f"   [!] {text}")
    util.info(
        f"   {len(unexplained)} unexplained issue(s), {len(data['issues']) - len(unexplained)} expected; "
        f"{pending} item(s) need a manual check"
    )
    util.info(f"   report: {report / 'review.txt'}")
    return not unexplained


def cmd_review(args) -> None:
    if not shutil.which("fedora-review"):
        util.die("fedora-review is not installed (dnf install fedora-review)")
    pkgs = [
        p
        for s in build.build_stages(
            packages.select(args.root, args.crates, args.all), args.root
        )
        for p in s
    ]
    local = packages.local_packages(args.root)
    deps = build.local_deps([p for p in local.values() if p.version], args.root)
    results = (
        Path(args.localrepo or (config.CACHE_DIR / "mock-repo"))
        / "results"
        / args.chroot
    )
    failed = []
    for pkg in pkgs:
        closure, todo = set(), list(deps.get(pkg.crate, ()))
        while todo:
            d = todo.pop()
            if d not in closure:
                closure.add(d)
                todo += deps.get(d, ())
        if not review_one(pkg, args.chroot, results, closure, local):
            failed.append(pkg.crate)
    if failed:
        util.die(f"review needs attention for: {', '.join(failed)}")
