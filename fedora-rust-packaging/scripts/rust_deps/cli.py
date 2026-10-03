"""rust-deps command line: the parser and main()."""

from __future__ import annotations

from pathlib import Path
import argparse
import os
import signal
import sys
import urllib.error
import urllib.parse
import urllib.request
import xmlrpc.client

from . import build
from . import config
from . import coprlogs
from . import distgit
from . import doctor
from . import edits
from . import pkginit
from . import regen
from . import resolver
from . import review
from . import reviewplan
from . import reviewrequest
from . import reviewstatus
from . import srpm
from . import status
from . import targets
from . import tmt
from . import trial
from . import tui_app
from . import update
from . import util


def build_parser() -> argparse.ArgumentParser:
    """The whole CLI surface, built once; 'tui' generates its forms from it, so
    the UI can never drift from the command line."""
    # the tool description is the package docstring, not this module's
    p = argparse.ArgumentParser(
        prog="rust-deps",
        description=sys.modules[__package__].__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get(config.ROOT_ENV, ".")),
        help=f"directory holding one sub-directory per package (default: ${config.ROOT_ENV}, else the current directory)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_sel(sp):
        sp.add_argument(
            "crates", nargs="*", help="crate names (sub-directories of --root)"
        )
        sp.add_argument("--all", action="store_true", help="all packages under --root")

    sp = sub.add_parser(
        "resolve", help="list crates missing from Fedora for a crate or a Cargo.toml"
    )
    sp.add_argument("crates", nargs="*", metavar="CRATE[@REQ]")
    sp.add_argument(
        "--manifest",
        action="append",
        help="seed from the dependencies of this Cargo.toml",
    )
    sp.add_argument(
        "--ignore-local",
        action="store_true",
        help="do not count packages under --root as available",
    )
    sp.add_argument(
        "--local-root",
        action="append",
        metavar="DIR",
        help="another package tree whose packages count as available (repeatable)",
    )
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "-r",
        "--chroot",
        dest="target",
        metavar="CHROOT",
        help="check against the crate repositories of this COPR/mock chroot "
        "(e.g. fedora-44-x86_64, rhel+epel-10-x86_64) instead of the host's",
    )
    sp.add_argument(
        "--refresh", action="store_true", help="refresh the cached Fedora crate list"
    )
    sp.set_defaults(func=resolver.cmd_resolve)

    sp = sub.add_parser(
        "init", help="create package directories, config, edits, spec; then trial"
    )
    sp.add_argument("crates", nargs="+", metavar="CRATE[@REQ]")
    sp.add_argument(
        "--recursive", action="store_true", help="also create every missing dependency"
    )
    sp.add_argument(
        "--force",
        action="store_true",
        help="re-initialize existing packages (overwrites config)",
    )
    sp.add_argument(
        "--no-trial", action="store_true", help="skip the test discovery run"
    )
    sp.add_argument(
        "--apply-tests", action="store_true", help="write the suggested [tests] table"
    )
    sp.add_argument(
        "--compat",
        action="store_true",
        help="create compat packages (rust-<crate><major.minor>), installable next to another "
        "version of the crate",
    )
    sp.add_argument(
        "-r",
        "--chroot",
        dest="target",
        metavar="CHROOT",
        help="check against the crate repositories of this COPR/mock chroot "
        "(e.g. fedora-44-x86_64, rhel+epel-10-x86_64) instead of the host's",
    )
    sp.add_argument("--refresh", action="store_true")
    sp.set_defaults(func=pkginit.cmd_init)

    sp = sub.add_parser("regen", help="regenerate spec and patches with rust2rpm")
    with_sel(sp)
    g = sp.add_mutually_exclusive_group()
    g.add_argument(
        "--latest", action="store_true", help="update to the newest crates.io release"
    )
    g.add_argument("--version", help="package this version")
    g.add_argument(
        "--crate-file",
        type=Path,
        metavar="PATH",
        help="generate from a local .crate (e.g. 'cargo package' output of an unreleased version)",
    )
    g = sp.add_mutually_exclusive_group()
    g.add_argument(
        "--compat",
        action="store_true",
        default=None,
        help="make it a compat package (rust-<crate><major.minor>); kept on later regens",
    )
    g.add_argument(
        "--no-compat",
        dest="compat",
        action="store_false",
        help="make it a regular package again",
    )
    sp.set_defaults(func=regen.cmd_regen)

    sp = sub.add_parser(
        "update",
        help="update packages to a new upstream release: regen, move upstream "
        "sources to the release's commit, check edits and dependents, trial",
    )
    with_sel(sp)
    sp.add_argument(
        "--version",
        help="update to this release (default: the newest; for a compat package, "
        "the newest of its series)",
    )
    sp.add_argument(
        "--no-trial",
        action="store_true",
        help="skip running the spec's tests with cargo",
    )
    sp.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="only show the updates, in build order",
    )
    sp.add_argument(
        "-r",
        "--chroot",
        dest="target",
        metavar="CHROOT",
        help="check the edits against this COPR/mock chroot's crates instead of the host's",
    )
    sp.add_argument(
        "--refresh", action="store_true", help="refresh the cached Fedora crate list"
    )
    sp.set_defaults(func=update.cmd_update)

    sp = sub.add_parser("trial", help="cargo test the patched crate like %%check would")
    with_sel(sp)
    sp.add_argument(
        "--discover",
        action="store_true",
        help="try all test targets and suggest a [tests] table",
    )
    sp.add_argument(
        "--apply",
        action="store_true",
        help="with --discover: write the suggestion to rust2rpm.toml",
    )
    sp.add_argument(
        "--timeout",
        type=int,
        default=trial.TRIAL_TIMEOUT,
        help="seconds per cargo invocation (default %(default)s)",
    )
    sp.add_argument(
        "--online",
        action="store_true",
        help="allow network access during tests (mock builds have none, so the default is offline)",
    )
    sp.set_defaults(func=trial.cmd_trial)

    sp = sub.add_parser(
        "srpm", help="fetch sources, build SRPMs, check %%prep and rpmlint"
    )
    with_sel(sp)
    sp.add_argument("--no-prep", action="store_true")
    sp.set_defaults(func=srpm.cmd_srpm)

    sp = sub.add_parser("order", help="print build stages (dependency order)")
    with_sel(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=build.cmd_order)

    sp = sub.add_parser(
        "mock-chain", help="build SRPMs with 'mock --chain' in dependency order"
    )
    with_sel(sp)
    sp.add_argument("-r", "--chroot", default="fedora-rawhide-x86_64")
    sp.add_argument("--localrepo")
    sp.add_argument("-n", "--dry-run", action="store_true")
    sp.set_defaults(func=build.cmd_mock_chain)

    sp = sub.add_parser(
        "copr", help="submit SRPMs to a COPR project, chained in dependency order"
    )
    with_sel(sp)
    sp.add_argument("--project", required=True, help="COPR project, e.g. user/project")
    sp.add_argument("-r", "--chroot", action="append", default=[])
    sp.add_argument("-n", "--dry-run", action="store_true")
    sp.add_argument(
        "--wait", action="store_true", help="wait until all builds have finished"
    )
    sp.add_argument(
        "--force",
        action="store_true",
        help="submit even if 'check-targets' finds BuildRequires that would not resolve",
    )
    sp.add_argument(
        "--retry-failed",
        action="store_true",
        help="resubmit only the chroots whose failure can succeed now (see copr-status), "
        "and packages with no build of their current version",
    )
    sp.set_defaults(func=build.cmd_copr)

    sp = sub.add_parser(
        "copr-log",
        help="fetch and summarize a COPR build log: errors, explained warnings",
    )
    sp.add_argument("crate")
    sp.add_argument(
        "-r", "--chroot", required=True, help="COPR chroot, e.g. rhel+epel-10-x86_64"
    )
    sp.add_argument("--project", required=True, help="COPR project, e.g. user/project")
    sp.add_argument(
        "--grep",
        metavar="REGEX",
        help="print matching log lines instead of the summary",
    )
    sp.add_argument(
        "--tail",
        type=int,
        metavar="N",
        help="print the last N lines instead of the summary",
    )
    sp.add_argument(
        "--errors",
        type=int,
        default=15,
        metavar="N",
        help="error lines to show (default %(default)s)",
    )
    sp.set_defaults(func=coprlogs.cmd_copr_log)

    sp = sub.add_parser(
        "check-targets",
        help="check that every BuildRequires resolves in each target chroot (before 'copr')",
    )
    with_sel(sp)
    sp.add_argument(
        "-r", "--chroot", action="append", default=[], help="target chroot (repeatable)"
    )
    sp.add_argument(
        "--project",
        help="COPR project whose chroots are the targets, e.g. user/project",
    )
    sp.add_argument(
        "-q", "--quiet", action="store_true", help="show only packages with problems"
    )
    sp.add_argument(
        "--refresh",
        action="store_true",
        help="refresh the cached crate lists of the targets",
    )
    sp.set_defaults(func=targets.cmd_check_targets)

    sp = sub.add_parser(
        "copr-status",
        help="diagnose the COPR builds of the current versions per chroot (read-only)",
    )
    with_sel(sp)
    sp.add_argument("--project", required=True, help="COPR project, e.g. user/project")
    sp.add_argument(
        "-r",
        "--chroot",
        action="append",
        default=[],
        help="only these chroots (repeatable)",
    )
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "--refresh",
        action="store_true",
        help="refresh the cached crate lists of the targets",
    )
    sp.add_argument(
        "--wait",
        action="store_true",
        help="wait until no chroot is building, then report",
    )
    sp.add_argument(
        "--interval",
        type=int,
        default=120,
        metavar="S",
        help="with --wait: seconds between checks (default %(default)s)",
    )
    sp.set_defaults(func=targets.cmd_copr_status)

    sp = sub.add_parser(
        "tmt", help="run the generated tmt tests in a container against the COPR builds"
    )
    with_sel(sp)
    sp.add_argument(
        "--project",
        required=True,
        help="COPR project holding the builds, e.g. user/project",
    )
    sp.add_argument(
        "-r",
        "--chroot",
        default="fedora-rawhide-x86_64",
        help="Fedora chroot whose release the container runs (default %(default)s)",
    )
    sp.add_argument("-n", "--dry-run", action="store_true")
    sp.set_defaults(func=tmt.cmd_tmt)

    sp = sub.add_parser(
        "review", help="run fedora-review on each package (after mock-chain)"
    )
    with_sel(sp)
    sp.add_argument("-r", "--chroot", default="fedora-rawhide-x86_64")
    sp.add_argument(
        "--localrepo", help="mock-chain local repository holding the dependencies' RPMs"
    )
    sp.set_defaults(func=review.cmd_review)

    sp = sub.add_parser(
        "review-request",
        help="draft or file the Bugzilla review requests (after review and copr)",
    )
    with_sel(sp)
    sp.add_argument(
        "--project",
        required=True,
        help="COPR project holding the builds, e.g. user/project",
    )
    sp.add_argument(
        "-r",
        "--chroot",
        default="fedora-rawhide-x86_64",
        help="COPR chroot to link the spec from",
    )
    sp.add_argument("--fas", help="your Fedora account name (default: $RUST_DEPS_FAS)")
    sp.add_argument(
        "--needs-sponsor",
        action="store_true",
        help="block FE-NEEDSPONSOR (you are not in the packager group yet)",
    )
    sp.add_argument(
        "--comment", help="extra text for the comment that posts updated URLs"
    )
    sp.add_argument(
        "--koji-task",
        action="append",
        default=[],
        metavar="CRATE=TASK",
        help="link a successful Koji scratch build (task ID or taskinfo URL) of the crate's SRPM",
    )
    sp.add_argument(
        "--file",
        action="store_true",
        help="file new requests / post updated URLs in Bugzilla (default: only write drafts)",
    )
    sp.set_defaults(func=reviewrequest.cmd_review_request)

    sp = sub.add_parser(
        "adopt",
        help="take over Fedora's dist-git packaging of packages that are in Fedora",
    )
    with_sel(sp)
    sp.add_argument(
        "--branch", default="rawhide", help="dist-git branch (default %(default)s)"
    )
    sp.add_argument(
        "--mark-only",
        action="store_true",
        help="keep the local packaging (e.g. tuned for older targets); only mark the package as "
        "in Fedora: no review request, built where a target lacks this version",
    )
    sp.add_argument("-n", "--dry-run", action="store_true")
    sp.set_defaults(func=distgit.cmd_adopt)

    sp = sub.add_parser(
        "dist-git",
        help="prepare the dist-git update of adopted packages: a local commit, "
        "or with --push, in your fork (public)",
    )
    with_sel(sp)
    sp.add_argument(
        "--dir",
        type=Path,
        help=f"where the dist-git checkouts are (default: ${distgit.DIST_GIT_ENV})",
    )
    sp.add_argument(
        "--branch",
        help="dist-git branch to update (default: the one each package was adopted "
        "from, in its dist-git.toml)",
    )
    sp.add_argument(
        "--push",
        action="store_true",
        help="fork the repository (once), upload the sources to the lookaside cache, and push the "
        "prepared branch to the fork; prints the link to open the pull request",
    )
    sp.add_argument("-n", "--dry-run", action="store_true")
    sp.set_defaults(func=distgit.cmd_dist_git)

    sp = sub.add_parser(
        "review-plan",
        help="what to submit for review, in which order, and which drafts are "
        "ready (read-only)",
    )
    with_sel(sp)
    sp.add_argument(
        "--project",
        help="COPR project holding the builds; checks the published spec and SRPM",
    )
    sp.add_argument(
        "-r",
        "--chroot",
        default="fedora-rawhide-x86_64",
        help="COPR chroot to check (default %(default)s)",
    )
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=reviewplan.cmd_review_plan)

    sp = sub.add_parser(
        "review-status",
        help="show the state of the Bugzilla review tickets (read-only)",
    )
    with_sel(sp)
    sp.add_argument(
        "--comments",
        type=int,
        default=5,
        metavar="N",
        help="show up to N unanswered comments per ticket (default %(default)s)",
    )
    sp.add_argument(
        "--record",
        action="store_true",
        help="record tickets found by summary search in review-request.json",
    )
    sp.add_argument(
        "--user",
        metavar="LOGIN",
        help="report all review tickets filed by this Bugzilla user (e.g. a FAS name) instead; "
        "state and threads are kept in ~/.cache/rust-packaging-tools/review-status/LOGIN",
    )
    sp.add_argument(
        "--closed", action="store_true", help="with --user: include closed tickets"
    )
    sp.set_defaults(func=reviewstatus.cmd_review_status)

    sp = sub.add_parser("doctor", help="check that the required tools are installed")
    sp.set_defaults(func=doctor.cmd_doctor)

    sp = sub.add_parser(
        "status", help="show local packages vs. crates.io and Fedora/EPEL releases"
    )
    with_sel(sp)
    sp.add_argument(
        "-r",
        "--chroot",
        dest="target",
        action="append",
        default=[],
        metavar="RELEASE",
        help="a column for this release or chroot instead of the host's (repeatable): "
        "fedora-44, fedora-rawhide, rhel+epel-10 (COPR's EPEL), epel-10, fedora-44-x86_64",
    )
    sp.add_argument(
        "--project",
        metavar="P",
        help="a column for each release of this COPR project's chroots",
    )
    sp.add_argument(
        "--all-versions", action="store_true", help="list every version a release has"
    )
    sp.add_argument(
        "--refresh",
        action="store_true",
        help="refresh the cached Fedora and crates.io data",
    )
    sp.set_defaults(func=status.cmd_status)

    sp = sub.add_parser("tui", help="text UI over all commands (needs python3-textual)")
    sp.set_defaults(func=tui_app.cmd_tui)

    return p


def main() -> None:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # quiet exit when piped into head
    if (
        os.environ.get(config.EDITOR_ENV)
        and len(sys.argv) == 2
        and sys.argv[1].endswith("Cargo.toml")
    ):
        edits.editor_mode(sys.argv[1])
        return

    p = build_parser()
    args = p.parse_args()
    args.root = args.root.resolve()
    if args.cmd not in ("doctor", "init") and not args.root.is_dir():
        # 'init' creates the tree; 'doctor' reports on it
        util.die(
            f"{args.root} is not a directory (wrong --root or ${config.ROOT_ENV}? 'rust-deps doctor' checks the setup)"
        )
    try:
        args.func(args)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        util.die(f"network request failed: {exc}")
    except xmlrpc.client.Error as exc:
        util.die(f"Koji/XML-RPC request failed: {exc}")
    except KeyboardInterrupt:
        util.die("interrupted")
