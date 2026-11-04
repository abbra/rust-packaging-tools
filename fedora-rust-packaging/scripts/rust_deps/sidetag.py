"""rust-deps sidetag: build a package chain in a Koji side-tag.

A chain that must move together (a multi-build update) is built on a side-tag:
Koji builds stage by stage against the side-tag's own repository, so a later
package sees the earlier one, and no half-finished Rawhide update exists while
the chain is still running.  Bodhi turns the side-tag into a single update once
every build is done.

The builds come from dist-git commits, not local SRPMs: Koji clones only a
package's own repository, so the update has to be in it — its pull request
merged, or its branch pushed there.
"""

from __future__ import annotations

from pathlib import Path
import json
import re
import shutil
import urllib.parse
import xmlrpc.client

from . import build
from . import distgit
from . import packages
from . import reviewrequest
from . import reviewstatus
from . import util
from . import versions

# Koji clones anonymously; the ssh remotes fedpkg creates are rewritten to this.
ANONGIT = "https://src.fedoraproject.org"
KOJI_HUB = "https://koji.fedoraproject.org/kojihub"
KCLI = ["koji"]  # auth: Kerberos, as the user's own koji/fedpkg calls


def anon_git_url(url: str) -> str | None:
    """The anonymous https clone URL of a dist-git repository from any of its remotes."""
    m = re.match(r"(?:ssh://[^@/]+@[^/]+/|git@[^:]+:|https?://[^/]+/)(.+)$", url)
    return f"{ANONGIT}/{m.group(1)}" if m else None


def koji_task_id(out: str) -> int:
    """The task ID koji's build command prints ('Created task: 123')."""
    if not (m := re.search(r"Created task: (\d+)", out)):
        util.die(f"cannot parse the task id:\n{out}")
    return int(m.group(1))


def update_state(pkg: packages.LocalPackage) -> dict | None:
    """The prepared dist-git update: what a side-tag build of this package would test."""
    f = pkg.dir / distgit.UPDATE_STATE
    if not f.exists():
        util.action(f"{pkg.crate}: no dist-git update; 'dist-git {pkg.crate}' first")
        return None
    state = json.loads(f.read_text())
    if state.get("version") != pkg.version:
        util.action(
            f"{pkg.crate}: the prepared update is {state.get('version')}, the package here "
            f"is {pkg.version}; 'dist-git {pkg.crate}' again"
        )
        return None
    if not state.get("pushed"):
        util.action(
            f"{pkg.crate}: the update is not pushed; 'dist-git --push {pkg.crate}' first"
        )
        return None
    return state


def dist_git_ref(checkout: Path, origin: str, ref: str) -> str | None:
    """The commit dist-git has for that branch (anonymously; nothing local changes)."""
    out = util.run(
        ["git", "ls-remote", origin, f"refs/heads/{ref}"], capture_output=True
    ).stdout.split()
    return out[0] if out else None


def dist_git_differs(checkout: Path, ref: str, pkg: packages.LocalPackage) -> str | None:
    """Why that dist-git ref would not test our packaging, or None if it would.

    Comparing the spec itself catches everything: an un-updated branch (Version),
    and a packaging-only change, which keeps the version under %autorelease.
    FETCH_HEAD only, no local ref is moved.
    """
    if util.run(["git", "-C", str(checkout), "fetch", "-q", "origin", ref], capture_output=True).returncode:
        return f"cannot fetch {ref} from {checkout}"
    res = util.run(
        ["git", "-C", str(checkout), "show", f"FETCH_HEAD:{pkg.rpm_name}.spec"], capture_output=True
    )
    if res.returncode:
        return f"it has no {pkg.rpm_name}.spec"
    if res.stdout != pkg.spec.read_text():
        have = (re.search(r"^Version:\s+(\S+)", res.stdout, re.M) or [None, None])[1]
        return (
            f"it has {pkg.rpm_name}-{have}, not {pkg.version}"
            if have != pkg.version
            else "its spec is not the one generated here (a packaging change is not in it)"
        )
    return None


def build_url(pkg: packages.LocalPackage) -> str | None:
    """git+<dist-git>#<commit> of the update, as Koji can clone it.

    Koji clones only the package's own repository (a fork URL fails with
    'SCM … is not allowed'), so the update must be in dist-git already: its
    pull request merged, or the branch pushed there.
    """
    state = update_state(pkg)
    if not state:
        return None
    checkout, branch = Path(state["checkout"]), state["branch"]
    origin = distgit.git(checkout, "remote", "get-url", "origin", check=False).stdout.strip()
    if not origin:
        util.action(f"{pkg.crate}: {checkout} has no origin; 'dist-git {pkg.crate}' again")
        return None
    push = f"git -C {checkout} push origin {branch}:refs/heads/{branch}"
    for ref in [branch, state.get("target", "rawhide")]:
        if not (sha := dist_git_ref(checkout, origin, ref)):
            continue
        if why := dist_git_differs(checkout, ref, pkg):
            util.action(
                f"{pkg.crate}: dist-git {ref} would not test this packaging: {why}; merge the "
                f"pull request of {branch} or push it: {push}"
            )
            return None
        return f"git+{anon_git_url(origin) or origin}#{sha}"
    util.action(
        f"{pkg.crate}: no branch of {origin} has the update; Koji cannot build from a "
        f"fork — merge the pull request or push it: {push}"
    )
    return None


def base_tag(target: str) -> str:
    """The build tag a Koji build target takes its buildroots from."""
    hub = xmlrpc.client.ServerProxy(KOJI_HUB, allow_none=True)
    if not (t := hub.getBuildTarget(target)):
        util.die(f"Koji has no build target {target}")
    return t["build_tag_name"]


def my_sidetags(base: str) -> list[str]:
    """Side-tags of that base tag belonging to the running user, oldest first."""
    res = util.run(
        [*KCLI, "list-sidetags", "--basetag", base, "--mine"], capture_output=True
    )
    if res.returncode:
        util.die(f"koji list-sidetags failed (Kerberos ticket?):\n{res.stderr}")
    return sorted(res.stdout.split())


def create_sidetag(base: str, dry_run: bool) -> str:
    """A new side-tag for that base tag (public: it asks Koji for one)."""
    if dry_run:
        return f"<new side-tag of {base}>"
    res = util.run([*KCLI, "add-sidetag", base], capture_output=True)
    if res.returncode:
        util.die(f"koji add-sidetag {base} failed:\n{res.stderr}")
    return res.stdout.strip().splitlines()[-1]


def task_link(task: int) -> str:
    return util.link(f"https://koji.fedoraproject.org/koji/taskinfo?taskID={task}", str(task))


def submit_stage(
    target: str, stage: list[packages.LocalPackage], urls: dict[str, str], dry_run: bool
) -> list[int]:
    """Submit one stage's packages to the side-tag target; returns the task IDs."""
    tasks = []
    for pkg in stage:
        cmd = [*KCLI, "build", "--nowait", target, urls[pkg.crate]]
        util.info("$ " + " ".join(cmd))
        if dry_run:
            continue
        res = util.run(cmd, capture_output=True)
        if res.returncode:
            util.die(f"{pkg.rpm_name}: {res.stdout}{res.stderr}")
        task = koji_task_id(res.stdout)
        util.info(f"   {pkg.rpm_name}: task {task_link(task)}")
        tasks.append(task)
    return tasks


OPEN_STATES = ["NEW", "ASSIGNED", "MODIFIED", "ON_QA", "RELEASE_PENDING"]


def open_bugs(**query) -> list[dict]:
    """A Bugzilla search limited to tickets that are not closed yet."""
    q = urllib.parse.urlencode(
        {**query, "bug_status": OPEN_STATES, "query_format": "advanced"}, doseq=True
    )
    return reviewstatus.bz_rest(f"bug?{q}")["bugs"]


def tracker_bugs(pkgs: list[packages.LocalPackage]) -> list[int]:
    """Open '<rpm name>-<version> is available' bugs this update satisfies.

    Those are the release-tracking bugs people file for a new upstream release;
    naming them on the update lets Bodhi close them when it reaches stable.
    Only the ones we actually reach: an older version's bug is fixed here, a
    newer one is still open upstream work.
    """
    if not pkgs:
        return []
    bugs = []
    for bug in open_bugs(
        product="Fedora",
        component=[p.rpm_name for p in pkgs],
        include_fields="id,summary",
    ):
        for pkg in pkgs:
            if m := re.fullmatch(
                re.escape(pkg.rpm_name) + r"-([0-9][\w.~+-]*) is available", bug["summary"]
            ):
                wanted, have = versions.parse_version(m.group(1)), versions.parse_version(
                    pkg.version
                )
                if wanted and have and wanted <= have:
                    bugs.append(bug["id"])
    return bugs


def update_bugs(pkgs: list[packages.LocalPackage]) -> list[int]:
    """The Bugzilla tickets to name on the update, so Bodhi closes them when it
    reaches stable: the packages' open review tickets (this update is what
    completes a review) and the open release-tracking bugs it satisfies."""
    mine = {
        state["bug"] for state in map(reviewrequest.request_state, pkgs) if state.get("bug")
    }
    named = {b["id"] for b in open_bugs(id=sorted(mine), include_fields="id")} if mine else set()
    return sorted(named | set(tracker_bugs(pkgs)))


def cmd_sidetag(args) -> None:
    """Build the packages of a chain in one Koji side-tag, stage by stage."""
    if not shutil.which("koji"):
        util.die("koji is needed to build in a side-tag: dnf install koji")
    stages = build.build_stages(packages.select(args.root, args.crates, args.all), args.root)
    urls = {}
    for s in stages:
        for p in s:
            if url := build_url(p):
                urls[p.crate] = url
    if missing := [p.crate for s in stages for p in s if p.crate not in urls]:
        # a partial chain would build the later packages against the old versions of
        # the earlier ones: the whole chain moves together or not at all
        util.die(f"no buildable dist-git update for: {', '.join(missing)}")
    if not stages:
        util.die("no package here has a prepared dist-git update; run 'dist-git' first")
    base = base_tag(args.target)
    own = my_sidetags(base)
    if own and not args.new:
        tag = own[0]
        util.info(f"reusing side-tag {tag} (of {base}); --new for another one")
    else:
        tag = create_sidetag(base, args.dry_run)
        util.info(f"side-tag: {tag} (build target {tag})")
    for i, stage in enumerate(stages, 1):
        util.info(f"=== stage {i}: {' '.join(p.crate for p in stage)}")
        tasks = submit_stage(tag, stage, urls, args.dry_run)
        if args.dry_run or not args.wait:
            continue
        # a later stage's buildroot must contain the earlier one: wait for the repo
        for task in tasks:
            if util.run([*KCLI, "watch-task", str(task)]).returncode:
                util.die(f"task {task_link(task)} failed; fix and resubmit this stage")
        if i == len(stages):
            continue
        # the next stage's buildroot must contain this stage's builds, so ask for a
        # repository current with the tag (all of them); --request is the fast path
        wait = [*KCLI, "wait-repo", "--request", tag]
        util.info("$ " + " ".join(wait))
        if util.run(wait).returncode:
            util.die(f"no repository of {tag} has the builds of stage {i}")
    named = sorted(
        {
            *update_bugs([p for s in stages for p in s]),
            *(int(b) for b in (args.bugs or "").split(",") if b.strip()),
        }
    )
    if named:
        util.info(
            "The update closes: "
            + ", ".join(util.link(reviewstatus.bug_url(b), str(b)) for b in named)
        )
    cmd = f"bodhi updates new --from-tag {tag} --type enhancement --notes '<what and why>'"
    if named:
        cmd += " --bugs " + ",".join(map(str, named))
    if args.dry_run:
        util.info("$ " + cmd)
        return
    util.info(
        "Builds are in the side-tag. When they are all done, one update covers them:\n"
        f"  {cmd}\n"
        "Bodhi closes the named bugs when the update reaches stable.\n"
        f"Status: koji list-tagged {tag};  Bodhi removes the tag when the update is pushed."
    )
