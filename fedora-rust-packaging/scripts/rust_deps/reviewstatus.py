"""rust-deps review-status: Bugzilla review state."""

from __future__ import annotations

from pathlib import Path
import json
import re
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request

from . import build
from . import config
from . import packages
from . import review
from . import reviewrequest
from . import util

# accounts that comment automatically (repository creation, Bodhi, release monitoring)
BZ_BOTS = {
    "fedora-admin-xmlrpc",
    "updates",
    "upstream-release-monitoring",
    "fedora-review-bot",
}
# fedora-review-service: rebuilds the posted SRPM in COPR and runs fedora-review on it
REVIEW_BOT = "fedora-review-bot"


def bug_url(bug_id: int | str, comment: int | None = None) -> str:
    if comment is not None:
        return (
            f"https://{reviewrequest.BUGZILLA_HOST}/show_bug.cgi?id={bug_id}#c{comment}"
        )
    return f"https://{reviewrequest.BUGZILLA_HOST}/{bug_id}"


def copr_build_url(build_id: int | str) -> str:
    return f"{reviewrequest.COPR_URL}/coprs/build/{build_id}"


BUG_FIELDS = (
    "id,summary,status,resolution,creator,assigned_to,flags,whiteboard,"
    "blocks,depends_on,last_change_time"
)


def bz_rest(path: str) -> dict:
    return json.loads(
        reviewrequest._get(f"https://{reviewrequest.BUGZILLA_HOST}/rest/{path}")
    )


def find_review_bug(rpm_name: str) -> dict | None:
    """The open or newest review ticket of a package, searched by summary."""
    title = f"Review Request: {rpm_name} - "
    query = urllib.parse.urlencode(
        {
            "product": "Fedora",
            "component": "Package Review",
            "summary": title,
            "include_fields": BUG_FIELDS,
        }
    )
    bugs = [
        b for b in bz_rest(f"bug?{query}")["bugs"] if b["summary"].startswith(title)
    ]
    bugs.sort(key=lambda b: (b["status"] != "CLOSED", b["id"]))
    return bugs[-1] if bugs else None


def review_flag(bug: dict) -> tuple[str, str]:
    for f in bug.get("flags", []):
        if f["name"] == "fedora-review":
            return f["status"], f.get("setter", "")
    return "", ""


def review_status_one(
    pkg: packages.LocalPackage, state: dict, record: bool, show: int
) -> tuple[str, int | None]:
    """Find the review ticket of a local package, print its state; return the next step."""
    rpm_name = pkg.rpm_name
    if state.get("bug"):
        bug = bz_rest(f"bug/{state['bug']}?include_fields={BUG_FIELDS}")["bugs"][0]
    else:
        bug = find_review_bug(pkg.rpm_name)
        if not bug:
            util.info(f"== {rpm_name}: no review ticket")
            return "file one with 'review-request --file'", None
        util.info(
            f"== {rpm_name}: found {util.link(bug_url(bug['id']), 'bug ' + str(bug['id']))} (not filed with review-request)"
        )
        if record:
            state = {"bug": bug["id"]}
            (pkg.dir / reviewrequest.REQUEST_STATE).write_text(
                json.dumps(state, indent=2) + "\n"
            )
    return (
        review_ticket_report(
            bug, rpm_name, show, pkg.dir / f"review-bug-{bug['id']}.txt", pkg
        ),
        bug["id"],
    )


def review_ticket_report(
    bug: dict,
    rpm_name: str,
    show: int,
    log: Path,
    pkg: packages.LocalPackage | None = None,
    prev: dict | None = None,
    record: dict | None = None,
) -> str:
    """Print the state of one review ticket; return the next step.

    With a local package, also compare the posted SRPM with it and phrase the
    next steps with rust-deps commands.  With prev (the record of an earlier
    check), report what changed since and mark new comments; record is filled
    with what the next check compares against.
    """
    url = bug_url(bug["id"])
    submitter = (
        bug["creator_detail"]["name"] if "creator_detail" in bug else bug["creator"]
    )
    reviewer = bug.get("assigned_to_detail", {}).get("name", bug["assigned_to"])
    flag, setter = review_flag(bug)
    status = bug["status"] + (f" {bug['resolution']}" if bug["resolution"] else "")
    util.info(f"== {rpm_name}: {url}")
    util.info(
        f"   {status}; fedora-review{flag or ' (not set)'}"
        + (f" by {setter}" if setter else "")
        + ("; no reviewer yet" if reviewer == "nobody" else f"; reviewer {reviewer}")
        + f"; last change {bug['last_change_time'][:10]}"
    )
    if prev:
        changed = [
            f"{k} {prev.get(k) or '-'} -> {v or '-'}"
            for k, v in (
                ("status", status),
                ("fedora-review", flag),
                ("reviewer", reviewer),
            )
            if prev.get(k, v) != v
        ]
        if changed:
            util.info(
                f"   changed since the check on {prev['checked'][:10]}: {'; '.join(changed)}"
            )
    if bug.get("whiteboard"):
        util.info(f"   whiteboard: {bug['whiteboard']}")
    if reviewrequest.FE_NEEDSPONSOR in bug.get("blocks", []):
        util.info("   blocks FE-NEEDSPONSOR (a sponsor is needed)")
    for dep in bug.get("depends_on", []):
        d = bz_rest(f"bug/{dep}?include_fields=id,summary,status,resolution,flags")[
            "bugs"
        ]
        if d:
            d = d[0]
            dflag, _ = review_flag(d)
            util.info(
                f"   depends on {util.link(bug_url(d['id']), str(d['id']))}: {d['summary'][:60]} "
                f"[{d['status']}{' ' + d['resolution'] if d['resolution'] else ''}"
                f"{', fedora-review' + dflag if dflag else ''}]"
            )
    needinfo = [
        f
        for f in bug.get("flags", [])
        if f["name"] == "needinfo" and f["status"] == "?"
    ]
    for f in needinfo:
        util.info(f"   NEEDINFO from {f.get('requestee', '?')}, asked by {f['setter']}")

    comments = bz_rest(f"bug/{bug['id']}/comment")["bugs"][str(bug["id"])]["comments"]
    log.write_text(
        "".join(
            f"=== #{c['count']} {c['creator']} {c['time']}\n{c['text']}\n\n"
            for c in comments
        )
    )
    last_own = max(
        (i for i, c in enumerate(comments) if c["creator"] == submitter), default=0
    )
    unanswered = [c for c in comments[last_own + 1 :] if c["creator"] not in BZ_BOTS]
    seen = prev.get("comments", 0) if prev else len(comments)
    if prev and len(comments) > seen:
        util.info(
            f"   {len(comments) - seen} new comment(s) since the check on {prev['checked'][:10]}"
        )
    if record is not None:
        record.update(
            status=status,
            **{"fedora-review": flag},
            reviewer=reviewer,
            comments=len(comments),
        )
    # the review is over once approved (RELEASE_PENDING: the dist-git repository
    # exists) or closed; its comments are usually the review itself: show only new ones
    finished = flag == "+" or bug["status"] in ("RELEASE_PENDING", "CLOSED")
    quiet = finished
    shown = [c for c in unanswered[-show:] if not quiet or c["count"] >= seen]
    if unanswered and not shown:
        util.info(
            f"   review finished; its {len(unanswered)} earlier comment(s) need nothing (thread in {log})"
            if finished
            else f"   {len(unanswered)} earlier comment(s) after the submitter's last one (in {log})"
        )
    if shown:
        util.info(
            f"   {len(unanswered)} comment(s) since the submitter's last one (all comments: {log}):"
        )
        for c in shown:
            lines = c["text"].strip().splitlines()
            util.info(
                f"   --- {util.link(bug_url(bug['id'], c['count']), '#' + str(c['count']))} {c['creator']} {c['time'][:10]}"
                + (" [new]" if c["count"] >= seen else "")
            )
            util.info(textwrap.indent("\n".join(lines[:40]), "      "))
            if len(lines) > 40:
                util.info(f"      [... {len(lines) - 40} more lines in {log}]")

    # the bot's findings no longer matter for a finished review
    bot_problem = (
        None if finished else review_bot_result(pkg, comments, submitter, bug["id"])
    )

    outdated = False
    if pkg:
        posted = re.findall(
            r"SRPM URL:\s*(\S+)",
            "\n".join(c["text"] for c in comments if c["creator"] == submitter),
        )
        local_nvr = (
            f"{rpm_name}-{reviewrequest.spec_query(pkg.spec, '%{version}-%{release}')}"
        )
        outdated = bool(posted) and local_nvr not in posted[-1]
        if outdated:
            util.info(
                f"   the last posted SRPM is {posted[-1].rsplit('/', 1)[-1]}, the local package is {local_nvr}"
            )

    if bug["status"] == "CLOSED":
        if bug["resolution"] in ("NEXTRELEASE", "RAWHIDE", "ERRATA", "CURRENTRELEASE"):
            return "done: the package is in Fedora"
        return f"closed as {bug['resolution']}; read the last comments"
    if flag == "-":
        return "rejected (fedora-review-); read the last comments"
    if needinfo and any(f.get("requestee") == submitter for f in needinfo):
        return "answer the reviewer's question (NEEDINFO on the submitter)"
    if unanswered and flag != "+":
        how = (
            "fix, regen, srpm, review, copr --wait, then "
            "'review-request --file --comment \"<what changed>\"'"
            if pkg
            else "fix the package and post new Spec/SRPM URLs"
        )
        return f"address the comments above: {how}" + (
            f"; also: {bot_problem}" if bot_problem else ""
        )
    if bot_problem and flag != "+":
        return bot_problem
    if outdated:
        return "post the new URLs: copr --wait, then 'review-request --file'"
    if flag == "+" or bug["status"] == "RELEASE_PENDING":
        # RELEASE_PENDING is set by the script that creates the dist-git repository
        if nvr := reviewrequest.koji_latest_build(rpm_name):
            return (
                f"built in Koji as {nvr} ; close the ticket as NEXTRELEASE "
                "(a Bodhi update that names it closes it when it reaches stable)"
            )
        if bug["status"] == "RELEASE_PENDING":
            return (
                f"approved, repository created: fedpkg clone {rpm_name}; import the SRPM, add the tmt "
                "files (.fmf plans tests) and build"
            )
        try:
            reviewrequest._get(f"https://src.fedoraproject.org/api/0/rpms/{rpm_name}")
            return (
                f"approved, repository exists: fedpkg clone {rpm_name}; import the SRPM, add the tmt "
                "files (.fmf plans tests) and build"
            )
        except urllib.error.HTTPError:
            return f"approved: fedpkg request-repo {rpm_name} {bug['id']}"
    if "NotReady" in (bug.get("whiteboard") or ""):
        return (
            "marked NotReady: address the issues, post new URLs, clear the whiteboard"
        )
    if flag == "?":
        return f"under review by {reviewer}; wait"
    return "waiting for a reviewer (ask on Package Review Swaps if it takes long)"


def review_bot_result(
    pkg: packages.LocalPackage | None, comments: list[dict], submitter: str, bug_id: int
) -> str | None:
    """Print the review bot's latest result; return a next step if it found a problem."""
    bot = [c for c in comments if c["creator"] == REVIEW_BOT]
    if not bot:
        return None
    last = bot[-1]
    last_urls = max(
        (
            i
            for i, c in enumerate(comments)
            if c["creator"] == submitter and "SRPM URL:" in c["text"]
        ),
        default=0,
    )
    stale = comments.index(last) < last_urls
    m = re.search(r"/coprs/build/(\d+)", last["text"])
    state = "?"
    if m:
        try:
            state = json.loads(
                reviewrequest._get(f"{reviewrequest.COPR_URL}/api_3/build/{m.group(1)}")
            )["state"]
        except urllib.error.URLError:
            state = (re.findall(r"\((\w+)\)", last["text"]) or ["?"])[0]
    comment = util.link(bug_url(bug_id, last["count"]), f"#{last['count']}")
    build = util.link(copr_build_url(m.group(1)), m.group(1)) if m else "?"
    util.info(
        f"   review bot ({comment}, {last['time'][:10]}): COPR build {build} {state}"
        + (
            " — older than the last posted URLs; the bot has not built them yet"
            if stale
            else ""
        )
    )
    if stale:
        return None
    if state == "failed":
        log = re.findall(r"(https://\S+builder-live\.log\S*)", last["text"])
        util.info(f"      build log: {log[0] if log else 'see the COPR build'}")
        return (
            "the review bot's COPR build failed: read its build log; missing BuildRequires must be "
            "review tickets listed in Depends On (or packages already in Fedora)"
        )
    template = re.findall(r"(https://\S+/fedora-review/review\.txt)", last["text"])
    if not template:
        return None
    try:
        issues = json.loads(
            reviewrequest._get(template[0].removesuffix(".txt") + ".json")
        )["issues"]
    except (urllib.error.URLError, ValueError):
        util.info(
            f"      review template: {template[0]} (no longer available for automatic checking)"
        )
        return None
    left = []
    for issue in issues:
        text = issue["text"] + (f" ({issue['note']})" if issue.get("note") else "")
        if review.expected_review_issue(issue, pkg):
            util.info(f"      [~] {text[:150]}")
        else:
            left.append(text)
            util.info(f"      [!] {text[:150]}")
    util.info(f"      template: {template[0]}")
    return "fix the issues the review bot found ([!] above)" if left else None


def user_review_bugs(user: str, include_closed: bool) -> list[dict]:
    """Package Review tickets filed by a Bugzilla user, newest first."""
    params = {
        "product": "Fedora",
        "component": "Package Review",
        "creator": user,
        "include_fields": BUG_FIELDS,
        "order": "bug_id DESC",
    }
    if not include_closed:
        params.update(f1="bug_status", o1="notequals", v1="CLOSED")
    bugs = []
    while True:
        # Bugzilla returns at most 20 bugs per request, whatever limit asks for,
        # and rejects an offset without a limit
        data = bz_rest(
            f"bug?{urllib.parse.urlencode({**params, 'limit': 20, 'offset': len(bugs)})}"
        )
        bugs += data["bugs"]
        if not data["bugs"] or len(bugs) >= data.get("total_matches", 0):
            break
    return [
        b
        for b in bugs
        if b.get("creator_detail", {}).get("name", user) == user
        and (include_closed or b["status"] != "CLOSED")
    ]


def review_status_user(user: str, include_closed: bool, show: int) -> None:
    """Report every review ticket filed by user; keep state in the cache."""
    cache = config.CACHE_DIR / "review-status" / user
    cache.mkdir(parents=True, exist_ok=True)
    state_file = cache / "state.json"
    old = json.loads(state_file.read_text()) if state_file.exists() else {}
    bugs = user_review_bugs(user, include_closed)
    if not bugs:
        util.info(
            f"no {'' if include_closed else 'open '}review tickets filed by {user}"
        )
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    new, summary = {}, []
    for bug in bugs:
        m = re.match(r"Review Request:\s*(\S+)", bug["summary"])
        rpm_name = m.group(1) if m else f"bug-{bug['id']}"
        prev = old.get(str(bug["id"]))
        record: dict = {}
        nxt = review_ticket_report(
            bug,
            rpm_name,
            show,
            cache / f"{bug['id']}-{rpm_name}.txt",
            prev=prev,
            record=record,
        )
        util.info(f"   next: {nxt}")
        fresh = prev is None or any(prev.get(k) != record.get(k) for k in record)
        new[str(bug["id"])] = {
            **record,
            "package": rpm_name,
            "next": nxt,
            "checked": now,
        }
        summary.append((str(bug["id"]), rpm_name, nxt, fresh))
    state_file.write_text(json.dumps({**old, **new}, indent=2) + "\n")
    util.info(
        f"\nsummary for {user} ('*' = new or changed since the last check; state in {cache}):"
    )
    refs = [util.link(bug_url(b), b) for b, *_ in summary]
    w1, w2 = max(util.visible_len(r) for r in refs), max(len(n) for _, n, *_ in summary)
    for ref, (_, name, nxt, fresh) in zip(refs, summary):
        util.info(
            f" {'*' if fresh else ' '} {util.ljust_visible(ref, w1)}  {name.ljust(w2)}  {nxt}"
        )


def cmd_review_status(args) -> None:
    if args.user:
        if args.crates or args.all:
            util.die(
                "--user reports the tickets of a Bugzilla user; do not name crates with it"
            )
        review_status_user(args.user, args.closed, args.comments)
        return
    pkgs = packages.select(args.root, args.crates, args.all)
    if all(p.crate_file and p.crate_file.exists() for p in pkgs):
        pkgs = [
            p for s in build.build_stages(pkgs, args.root) for p in s
        ]  # dependency order
    summary = []
    for pkg in pkgs:
        if pkg.limited and not reviewrequest.request_state(pkg).get("bug"):
            nxt = f"no review: {pkg.scope}"
            util.info(f"== {pkg.rpm_name} {pkg.version}\n   next: {nxt}")
            summary.append((pkg.rpm_name, "-", nxt))
            continue
        nxt, bug_id = review_status_one(
            pkg, reviewrequest.request_state(pkg), args.record, args.comments
        )
        util.info(f"   next: {nxt}")
        summary.append(
            (
                pkg.rpm_name,
                util.link(bug_url(bug_id), str(bug_id)) if bug_id else "-",
                nxt,
            )
        )
    if len(summary) > 1:
        util.info("\nsummary:")
        w1 = max(len(n) for n, *_ in summary)
        w2 = max(util.visible_len(b) for _, b, _ in summary)
        for n, b, nxt in summary:
            util.info(f"   {n.ljust(w1)}  {util.ljust_visible(b, w2)}  {nxt}")
