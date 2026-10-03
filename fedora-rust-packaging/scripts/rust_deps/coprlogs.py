"""rust-deps COPR build logs: fetch and summarize failures."""

from __future__ import annotations

from pathlib import Path
import gzip
import re
import urllib.error
import urllib.parse
import urllib.request

from . import build
from . import config
from . import copr
from . import edits
from . import packages
from . import reviewrequest
from . import reviewstatus
from . import util

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
WARNING_RE = re.compile(r"^warning: (.+)$")
WARNING_SUMMARY_RE = re.compile(
    r"^`[^`]+` \(.*\) generated \d+ warnings?|^\d+ warnings? emitted"
)


def explain_warning(msg: str, pkg: packages.LocalPackage) -> str | None:
    """Why a warning in a COPR build log is expected, or None if it is not known."""
    if m := re.match(r"unexpected `cfg` condition value: `([^`]+)`", msg):
        dropped = edits.load_edits(pkg.edits_file).get("drop-features", [])
        if m.group(1) in dropped:
            patch = pkg.dir / f"{pkg.crate}-fix-metadata.diff"
            now = (
                "the local patch declares it now; rebuild to silence it"
                if patch.exists() and "unexpected_cfgs" in patch.read_text()
                else "'regen' declares it; regen, srpm, rebuild"
            )
            return (
                f"{config.EDITS_FILE} drops feature '{m.group(1)}', but the code still has "
                f'#[cfg(feature = "{m.group(1)}")], compiled out as with the feature off. This build\'s '
                f"patch does not declare it as an expected cfg value: {now}. Do not keep an empty "
                "feature instead: it would give a +feature-devel subpackage that cannot build"
            )
        return "the code tests a cfg value its Cargo.toml does not declare; an upstream lint, harmless"
    if re.match(
        r"unexpected `cfg` condition name: `(docsrs|docsrs_\w+|nightly|coverage\w*|fuzzing|loom)`",
        msg,
    ):
        return "a cfg for docs.rs, nightly or test tooling; never set in Fedora builds"
    if msg.startswith("File listed twice: /usr/share/cargo/registry/"):
        return (
            "rust2rpm lists %doc/%license files inside %{crate_instdir}/, which %files also owns "
            "whole; standard for Rust packages"
        )
    if "/etc/hosts created as /etc/hosts.rpmnew" in msg:
        return "mock's buildroot setup, not the package"
    if msg.startswith("no (git) VCS found"):
        return "cargo reads the crate outside a git checkout; harmless"
    return None


def summarize_log(
    text: str, pkg: packages.LocalPackage
) -> tuple[list[str], list[str], list[tuple[str, int, list[str], str | None]]]:
    """(missing BuildRequires, build errors, [(warning, count, locations, explanation)])."""
    lines = [ANSI_RE.sub("", ln) for ln in text.splitlines()]
    missing = list(
        dict.fromkeys(
            copr.MissingReq.parse(m.group(1) or m.group(2)).label()
            for ln in lines
            if (m := copr.MISSING_RE.search(ln))
        )
    )
    errors = list(
        dict.fromkeys(
            ln.strip() for ln in lines if copr.BUILD_ERROR_RE.match(ln.strip())
        )
    )
    warnings: dict[str, tuple[int, list[str]]] = {}
    for i, ln in enumerate(lines):
        m = WARNING_RE.match(ln.strip())
        if not m or WARNING_SUMMARY_RE.match(m.group(1)):
            continue
        count, locs = warnings.get(m.group(1), (0, []))
        loc = next(
            (x.split("-->", 1)[1].strip() for x in lines[i + 1 : i + 4] if "-->" in x),
            None,
        )
        warnings[m.group(1)] = (
            count + 1,
            locs + ([loc] if loc and loc not in locs else []),
        )
    return (
        missing,
        errors,
        [(w, c, locs, explain_warning(w, pkg)) for w, (c, locs) in warnings.items()],
    )


def fetch_copr_log(
    build_id: int, chroot: str, result_url: str, active: bool
) -> Path | None:
    """A chroot's build log; a running build's live log is fetched again every time."""
    if not active:
        return copr.CoprState.fetch_log(build_id, chroot, result_url)
    try:
        data = reviewrequest._get(result_url.rstrip("/") + "/builder-live.log")
    except (urllib.error.URLError, OSError):
        return None
    if data[:2] == b"\x1f\x8b":  # served compressed
        data = gzip.decompress(data)
    path = config.CACHE_DIR / "copr-logs" / f"{build_id}-{chroot}.live.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def cmd_copr_log(args) -> None:
    pkg = packages.select(args.root, [args.crate], False)[0]
    state = copr.CoprState(args.project, args.root)
    evr = reviewrequest.spec_query(pkg.spec, "%{version}-%{release}")
    for build in state.builds(pkg, evr):
        bc = next(
            (
                c
                for c in copr.copr_api("build-chroot/list/", build_id=build["id"])[
                    "items"
                ]
                if c["name"] == args.chroot
            ),
            None,
        )
        if bc:
            break
    else:
        util.die(f"no COPR build of {pkg.crate} {evr} in {args.chroot}")
    blink = util.link(reviewstatus.copr_build_url(build["id"]), str(build["id"]))
    util.info(f"{pkg.crate} {evr}, build {blink}, {args.chroot}: {bc['state']}")
    if not bc.get("result_url"):
        util.die("no log yet: the build has not started")
    path = fetch_copr_log(
        build["id"], args.chroot, bc["result_url"], bc["state"] in copr.COPR_ACTIVE
    )
    if not path:
        util.die(f"cannot download the log from {bc['result_url']}")
    util.info(f"log: {path}")
    text = path.read_text(errors="replace")
    if args.grep:
        rx = re.compile(args.grep)
        for i, ln in enumerate(text.splitlines(), 1):
            if rx.search(ANSI_RE.sub("", ln)):
                util.info(f"{i:6}: {ANSI_RE.sub('', ln)}")
        return
    if args.tail:
        for ln in text.splitlines()[-args.tail :]:
            util.info(ANSI_RE.sub("", ln))
        return
    missing, errors, warnings = summarize_log(text, pkg)
    conflicts = copr.log_conflicts([ANSI_RE.sub("", ln) for ln in text.splitlines()])
    for c in conflicts:
        util.info(f"   conflict: {c.label()}")
    for m in missing:
        util.info(f"   missing BuildRequires: {m}")
    for e in errors[: args.errors]:
        util.info(f"   error: {e}")
    if len(errors) > args.errors:
        util.info(f"   … {len(errors) - args.errors} more error line(s); see --grep")
    by_reason: dict[str | None, list] = {}
    for w in warnings:
        by_reason.setdefault(w[3], []).append(w)
    for why, ws in sorted(by_reason.items(), key=lambda kv: kv[0] is None):
        shown = ws if why is None or len(ws) <= 2 else ws[:1]
        for w, count, locs, _ in shown:
            where = (
                f" ({', '.join(locs[:3])}{', …' if len(locs) > 3 else ''})"
                if locs
                else ""
            )
            util.info(f"   warning ×{count}: {w}{where}")
        if len(shown) < len(ws):
            util.info(f"   … and {len(ws) - len(shown)} more like it")
        util.info(
            f"      {'expected: ' + why if why else 'from the crate code; not a packaging problem unless the build fails'}"
        )
    if not (missing or errors or warnings or conflicts):
        util.info("   no errors or warnings")
    if missing or conflicts:
        util.info(
            f"   see 'copr-status --project {args.project} {pkg.crate}' for what to do"
        )
    elif errors and bc["state"] not in copr.COPR_ACTIVE:
        closure = local_closure(pkg, args.chroot, state.local)
        util.info(
            f"   reproduce: rust-deps mock-chain -r {copr.mock_chroot(args.chroot)} {' '.join(closure)}"
        )


def local_closure(
    pkg: packages.LocalPackage, chroot: str, local: dict[str, packages.LocalPackage]
) -> list[str]:
    """The package and the local packages it needs in that chroot, dependencies first."""
    order, seen = [], set()

    def visit(p: packages.LocalPackage) -> None:
        if p.crate in seen:
            return
        seen.add(p.crate)
        for d in sorted(build.build_deps(p)):
            if d in local and local[d].version and local[d].builds_in(chroot):
                visit(local[d])
        order.append(p.crate)

    visit(pkg)
    return order
