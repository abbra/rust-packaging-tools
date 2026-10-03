"""rust-deps review-request: draft and file the Bugzilla ticket."""

from __future__ import annotations

from pathlib import Path
import json
import os
import re
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import xmlrpc.client

from . import build
from . import config
from . import packages
from . import review
from . import reviewstatus
from . import util

COPR_URL = "https://copr.fedorainfracloud.org"
BUGZILLA_HOST = "bugzilla.redhat.com"
FE_NEEDSPONSOR = (
    177841  # tracker bug: review requests from contributors who need a sponsor
)
REQUEST_STATE = "review-request.json"  # {"bug": N, "spec_url": ..., "srpm_url": ...}
REQUEST_DRAFT = "review-request.txt"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def spec_query(spec: Path, fmt: str) -> str:
    res = util.run(
        ["rpmspec", "-q", "--srpm", "--define", "dist %{nil}", "--qf", fmt, str(spec)],
        capture_output=True,
    )
    if res.returncode:
        util.die(f"rpmspec failed on {spec}:\n{res.stderr}")
    return res.stdout


def request_state(pkg: packages.LocalPackage) -> dict:
    f = pkg.dir / REQUEST_STATE
    return json.loads(f.read_text()) if f.exists() else {}


def copr_review_urls(
    pkg: packages.LocalPackage, project: str, chroot: str
) -> tuple[dict | None, str | None]:
    """Spec/SRPM URLs of the latest succeeded COPR build of the local version, or (None, problem)."""
    owner, _, name = project.partition("/")
    rpm_name = pkg.rpm_name
    query = urllib.parse.urlencode(
        {
            "ownername": owner,
            "projectname": name,
            "packagename": rpm_name,
            "with_latest_succeeded_build": "true",
        }
    )
    try:
        data = json.loads(_get(f"{COPR_URL}/api_3/package/?{query}"))
    except urllib.error.HTTPError:
        return (
            None,
            f"COPR project {project} has no package {rpm_name}; build it with 'copr --wait'",
        )
    build = (data.get("builds") or {}).get("latest_succeeded")
    local = spec_query(pkg.spec, "%{version}-%{release}")
    if not build:
        return (
            None,
            f"no succeeded COPR build of {rpm_name} in {project}; build it with 'copr --wait'",
        )
    if build["source_package"]["version"] != local:
        return None, (
            f"latest succeeded COPR build {build['id']} is {build['source_package']['version']}, "
            f"the local package is {local}; rebuild it with 'copr --wait'"
        )
    try:
        bc = json.loads(
            _get(
                f"{COPR_URL}/api_3/build-chroot/?build_id={build['id']}&chrootname={chroot}"
            )
        )
    except urllib.error.HTTPError:
        return None, f"COPR build {build['id']} has no chroot {chroot}"
    if bc.get("state") != "succeeded":
        return (
            None,
            f"COPR build {build['id']} did not succeed in {chroot} ({bc.get('state')})",
        )
    spec_url = bc["result_url"].rstrip("/") + f"/{rpm_name}.spec"
    try:
        published = _get(spec_url).decode()
    except urllib.error.URLError as exc:
        return None, f"cannot download {spec_url}: {exc}"
    if published != pkg.spec.read_text():
        return (
            None,
            f"the spec in COPR build {build['id']} differs from the local one; rebuild with 'copr --wait'",
        )
    owner_path = f"g/{owner[1:]}" if owner.startswith("@") else owner
    return {
        "build": build["id"],
        "build_url": f"{COPR_URL}/coprs/{owner_path}/{name}/build/{build['id']}/",
        "spec_url": spec_url,
        "srpm_url": build["source_package"]["url"],
    }, None


def local_review_problem(pkg: packages.LocalPackage) -> str | None:
    """None if 'review' passed for the current SRPM, else what is wrong."""
    reports = sorted(
        (config.CACHE_DIR / "review").glob(
            f"*/{pkg.crate}/review-{pkg.rpm_name}/review.json"
        ),
        key=lambda f: f.stat().st_mtime,
    )
    if not reports:
        return "no local fedora-review result; run 'review' first"
    report = reports[-1]
    if report.stat().st_mtime < build.srpm_of(pkg).stat().st_mtime:
        return (
            "the local fedora-review result is older than the SRPM; run 'review' again"
        )
    issues = json.loads(report.read_text())["issues"]
    left = [i["text"] for i in issues if not review.expected_review_issue(i, pkg)]
    return (
        f"fedora-review found unexplained issues: {'; '.join(left)}" if left else None
    )


KOJI_URL = "https://koji.fedoraproject.org"


def koji_task_url(
    pkg: packages.LocalPackage, task: str
) -> tuple[str | None, str | None]:
    """URL of a successful Koji build task of the local SRPM, or (None, problem)."""
    m = re.fullmatch(r"(?:.*taskID=)?(\d+)", task.strip())
    if not m:
        return None, f"not a Koji task ID or taskinfo URL: {task}"
    info_ = xmlrpc.client.ServerProxy(
        f"{KOJI_URL}/kojihub", allow_none=True
    ).getTaskInfo(int(m.group(1)), True)
    if not info_:
        return None, f"Koji task {m.group(1)} does not exist"
    srpm = str(info_["request"][0]).rsplit("/", 1)[-1] if info_.get("request") else ""
    nvr = f"{pkg.rpm_name}-{spec_query(pkg.spec, '%{version}-%{release}')}"
    if info_["method"] != "build" or not srpm.startswith(nvr + "."):
        return (
            None,
            f"Koji task {m.group(1)} is not a build of {nvr} ({info_['method']} {srpm})",
        )
    if info_["state"] != 2:  # 2 = CLOSED (succeeded)
        return None, f"Koji task {m.group(1)} did not succeed"
    return f"{KOJI_URL}/koji/taskinfo?taskID={m.group(1)}", None


def koji_latest_build(name: str) -> str | None:
    """The newest completed Koji build of a package (a link labelled with its NVR), or None."""
    try:
        hub = xmlrpc.client.ServerProxy(f"{KOJI_URL}/kojihub", allow_none=True)
        pid = hub.getPackageID(name)
        if not pid:
            return None
        builds = hub.listBuilds(
            {"packageID": pid, "state": 1, "__starstar": True}
        )  # 1 = COMPLETE
    except (OSError, xmlrpc.client.Error):
        return None
    if not builds:
        return None
    newest = max(builds, key=lambda b: b["build_id"])
    return util.link(
        f"{KOJI_URL}/koji/buildinfo?buildID={newest['build_id']}",
        newest["nvr"],
        keep_label=True,
    )


def review_request_text(
    pkg: packages.LocalPackage, urls: dict, fas: str
) -> tuple[str, str]:
    summary = spec_query(pkg.spec, "%{summary}").strip()
    description = spec_query(pkg.spec, "%{description}").strip()
    upstream = spec_query(pkg.spec, "%{url}").strip()
    title = f"Review Request: {pkg.rpm_name} - {summary}"
    body = (
        f"Spec URL: {urls['spec_url']}\n"
        f"SRPM URL: {urls['srpm_url']}\n"
        f"Upstream URL: {upstream}\n\n"
        f"Description:\n{description}\n\n"
        f"Fedora Account System Username: {fas}\n\n"
        f"COPR build: {urls['build_url']}\n"
        + (f"Koji scratch build: {urls['koji_url']}\n" if urls.get("koji_url") else "")
    )
    return title, body


def bugzilla_client():
    try:
        import bugzilla
    except ImportError:
        util.die("filing needs python-bugzilla: dnf install python3-bugzilla")
    bz = bugzilla.Bugzilla(BUGZILLA_HOST)
    if not bz.logged_in:
        util.die(
            f"no Bugzilla API key: create one at https://{BUGZILLA_HOST}/userprefs.cgi?tab=apikey and put "
            f"it in ~/.config/python-bugzilla/bugzillarc as:\n  [{BUGZILLA_HOST}]\n  api_key = <key>"
        )
    return bz


def cmd_review_request(args) -> None:
    fas = args.fas or os.environ.get("RUST_DEPS_FAS")
    if not fas:
        util.die("give your Fedora account name with --fas (or RUST_DEPS_FAS)")
    pkgs = [
        p
        for s in build.build_stages(
            packages.select(args.root, args.crates, args.all), args.root
        )
        for p in s
    ]
    for p in (p for p in pkgs if p.limited):
        util.info(f"== {p.crate}: skipped; no review: {p.scope}")
    pkgs = [p for p in pkgs if not p.limited]
    local = packages.local_packages(args.root)
    deps = {
        c: {d for d in ds if not local[d].limited}
        for c, ds in build.local_deps(pkgs, args.root).items()
    }
    bz = bugzilla_client() if args.file else None
    koji_tasks = {}
    for item in args.koji_task:
        crate, sep, task = item.partition("=")
        if not sep or crate not in {p.crate for p in pkgs}:
            util.die(f"--koji-task expects CRATE=TASK for a selected crate: {item}")
        koji_tasks[crate] = task
    failed = []
    for pkg in pkgs:
        util.info(f"== review request {pkg.rpm_name} {pkg.version}")
        problem = local_review_problem(pkg)
        urls = None
        if not problem:
            urls, problem = copr_review_urls(pkg, args.project, args.chroot)
        if not problem and pkg.crate in koji_tasks:
            urls["koji_url"], problem = koji_task_url(pkg, koji_tasks[pkg.crate])
        if problem:
            util.action(f"{pkg.crate}: {problem}")
            failed.append(pkg.crate)
            continue
        state = request_state(pkg)
        title, body = review_request_text(pkg, urls, fas)
        (pkg.dir / REQUEST_DRAFT).write_text(f"Summary: {title}\n\n{body}")
        depends = []
        for d in sorted(deps.get(pkg.crate, ())):
            bug = request_state(local[d]).get("bug")
            if bug:
                depends.append(bug)
            elif args.file:
                util.action(
                    f"{pkg.crate}: depends on {local[d].rpm_name}, which has no review request yet; file that first"
                )
        if args.file and len(depends) < len(deps.get(pkg.crate, ())):
            failed.append(pkg.crate)
            continue
        if state.get("bug"):
            ticket = reviewstatus.bug_url(state["bug"])
            if (state.get("spec_url"), state.get("srpm_url")) == (
                urls["spec_url"],
                urls["srpm_url"],
            ):
                util.info(f"   {ticket} already has these URLs")
                continue
            comment = f"Spec URL: {urls['spec_url']}\nSRPM URL: {urls['srpm_url']}\n"
            if urls.get("koji_url"):
                comment += f"Koji scratch build: {urls['koji_url']}\n"
            if args.comment:
                comment += f"\n{args.comment}\n"
            if not args.file:
                util.info(
                    f"   update for {ticket} (post with --file):\n"
                    + textwrap.indent(comment, "      ")
                )
                continue
            bz.update_bugs([state["bug"]], bz.build_update(comment=comment))
            util.info(f"   posted the new URLs to {ticket}")
        else:
            if not args.file:
                util.info(
                    f"   draft written to {pkg.dir / REQUEST_DRAFT}"
                    + (
                        f"; depends on {', '.join(util.link(reviewstatus.bug_url(b), str(b)) for b in depends)}"
                        if depends
                        else ""
                    )
                )
                util.info(textwrap.indent(f"Summary: {title}\n\n{body}", "      "))
                continue
            created = bz.createbug(
                bz.build_createbug(
                    product="Fedora",
                    component="Package Review",
                    version="rawhide",
                    summary=title,
                    description=body,
                    depends_on=depends or None,
                    blocks=[FE_NEEDSPONSOR] if args.needs_sponsor else None,
                )
            )
            state = {"bug": created.id}
            util.info(f"   filed {reviewstatus.bug_url(created.id)}")
        state.update(spec_url=urls["spec_url"], srpm_url=urls["srpm_url"])
        (pkg.dir / REQUEST_STATE).write_text(json.dumps(state, indent=2) + "\n")
    if failed:
        util.die(f"no review request for: {', '.join(failed)}")
