"""rust-deps TUI overview: tree state, lifecycle stages, suggestions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
import json
import re

from . import packages
from . import pkginit
from . import reviewrequest


def _overview_packages(root: Path) -> list[packages.LocalPackage]:
    return sorted(packages.local_packages(root).values(), key=lambda p: p.crate)


def has_srpm(pkg: packages.LocalPackage) -> bool:
    return bool(
        pkg.version and list(pkg.dir.glob(f"{pkg.rpm_name}-{pkg.version}-*.src.rpm"))
    )


def has_tests(pkg: packages.LocalPackage) -> bool:
    return "[tests]" in (
        pkg.config_file.read_text() if pkg.config_file.exists() else ""
    )


_TESTS_SECTION_RE = re.compile(r"(?ms)^\[tests\]\n.*?(?=^\[|\Z)")
_COMMENTS_ARRAY_RE = re.compile(r"(?ms)^comments = (?:\[\]|\[.*?^\])")
_TOML_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def tests_table_comments(text: str) -> list[str]:
    """The comments entries of a rust2rpm.toml [tests] table, TOML-decoded."""
    m = _TESTS_SECTION_RE.search(text)
    if not m or not (am := _COMMENTS_ARRAY_RE.search(m.group(0))):
        return []
    return [json.loads(s) for s in _TOML_STRING_RE.findall(am.group(0))]


def replace_tests_comments(text: str, comments: list[str]) -> str:
    """Rewrite only the comments array of the [tests] table; the rest of the
    file, and the rest of the table, stays exactly as it is."""
    sm = _TESTS_SECTION_RE.search(text)
    if not sm:
        return text
    section = _COMMENTS_ARRAY_RE.sub(
        lambda _: "comments = " + pkginit.toml_list(comments), sm.group(0), count=1
    )
    return text[: sm.start()] + section + text[sm.end() :]


def has_tests_todo(pkg: packages.LocalPackage) -> bool:
    """A [tests] table whose reasons are still the TODO placeholders 'trial --apply' wrote."""
    return pkg.config_file.exists() and any(
        "TODO" in c for c in tests_table_comments(pkg.config_file.read_text())
    )


def tests_todo_crates(root: Path) -> list[str]:
    return sorted(
        p.crate for p in packages.local_packages(root).values() if has_tests_todo(p)
    )


@dataclass
class TuiStage:
    """One position of a package on the lifecycle graph: 'detect' is local file
    state, 'edges' are the commands that move a package out of this stage
    (command, flag field keys to pre-check, why)."""

    name: str
    detect: Callable[[packages.LocalPackage], bool]
    edges: list[tuple[str, list[str], str]]


# The Quick start pipeline as data.  First matching stage wins, so a package is
# suggested exactly the work that comes next for it; the build line (spec,
# tests, SRPM) is shared, and after it the tree branches: adopted packages go
# on to dist-git, target-limited ones to COPR, everything else onto the review
# line (review-plan → draft → filed).  The 'tests?' stage expands into the
# discovery chain that tui_trial_actions derives from a trial run's output.
TUI_STAGES = [
    TuiStage(
        "spec?",
        lambda p: not p.version,
        [("regen", [], "no generated spec for these packages")],
    ),
    TuiStage(
        "tests?",
        lambda p: not has_tests(p),
        [
            (
                "trial",
                ["discover"],
                "no [tests] in rust2rpm.toml: trial --discover picks what can run",
            )
        ],
    ),
    TuiStage(
        "tests-todo",
        has_tests_todo,
        [
            (
                "tests-edit",
                [],
                "the [tests] table still has TODO reasons: write the real ones",
            )
        ],
    ),
    TuiStage(
        "srpm?",
        lambda p: not has_srpm(p),
        [("srpm", [], "no SRPM built for the current version")],
    ),
    TuiStage(
        "adopted",
        lambda p: p.dist_git is not None,
        [("dist-git", [], "adopted from dist-git: prepare the update commit")],
    ),
    TuiStage(
        "targets",
        lambda p: p.targets is not None,
        [("copr", [], "limited to targets.toml: publish builds for those releases")],
    ),
    TuiStage(
        "filed",
        lambda p: (p.dir / reviewrequest.REQUEST_STATE).exists(),
        [("review-status", [], "the ticket is filed: see where it stands")],
    ),
    TuiStage(
        "draft",
        lambda p: (p.dir / reviewrequest.REQUEST_DRAFT).exists(),
        [("review-request", ["file"], "the draft is ready: file it on Bugzilla")],
    ),
    TuiStage(
        "review",
        lambda p: True,
        [("review-plan", [], "what to submit for review, in which order")],
    ),
]


def tui_stage(pkg: packages.LocalPackage) -> TuiStage:
    """Where a package sits on the graph: the first stage whose state matches."""
    for stage in TUI_STAGES:
        if stage.detect(pkg):
            return stage
    return TUI_STAGES[-1]  # 'review' matches everything


def tui_package_rows(root: Path) -> tuple[list[str], list[list[str]]]:
    """The Overview table: what each package under the root actually has and
    where it sits on the lifecycle graph."""
    headers = ["crate", "version", "spec", "patch", "srpm", "tests", "stage", "scope"]
    rows = []
    for pkg in _overview_packages(root):
        rows.append(
            [
                pkg.crate + (" (compat)" if pkg.compat else ""),
                pkg.version or "-",
                "yes" if pkg.version else "-",
                "yes" if (pkg.dir / f"{pkg.crate}-fix-metadata.diff").exists() else "-",
                "yes" if has_srpm(pkg) else "-",
                "yes" if has_tests(pkg) else "-",
                tui_stage(pkg).name,
                pkg.scope,
            ]
        )
    return headers, rows


def tui_suggestions(
    root: Path,
) -> list[tuple[str, list[str], list[str], dict[str, str], str]]:
    """Next steps implied by the tree state: (command, crates to preselect,
    flag field keys to pre-check, option values to prefill, why).
    Derived from the lifecycle graph: every package is placed on a stage, and
    each reached stage yields its edges with those crates picked.  Everything
    here is local."""
    pkgs = _overview_packages(root)
    if not pkgs:
        return [
            (
                "resolve",
                [],
                [],
                {},
                "nothing under this root yet: find the crates a project needs",
            ),
            ("init", [], [], {}, "or create packages directly"),
        ]
    at: dict[str, list[str]] = {}
    for p in pkgs:
        at.setdefault(tui_stage(p).name, []).append(p.crate)
    out: list[tuple[str, list[str], list[str], dict[str, str], str]] = []
    for stage in TUI_STAGES:
        crates = at.get(stage.name)
        if not crates:
            continue
        for cmd, flags, why in stage.edges:
            out.append((cmd, crates, flags, {}, f"{stage.name}: {why}"))
    if any(has_srpm(p) for p in pkgs):
        out.append(
            ("status", [], [], {}, "packaged vs. crates.io vs. the Fedora releases")
        )
    return out


# Blocks that name the package a suggestion belongs to: review-plan's numbered
# steps, review-status' and srpm's '== pkg' headers, copr-status' 'crate evr:'
# lines; tree-level bullets and stage headers clear the context.
_SUGGEST_STEP_RE = re.compile(
    r"^\s*\d+\. (\S+) (\S+)\s+(FILED|READY|NOT READY|update)$"
)
_SUGGEST_BLOCK_RE = re.compile(r"^== ([^\s:]+)(?::| \S+)")
_SUGGEST_COPR_RE = re.compile(
    r"^(\S+) (\S+): (?:build \S+|no COPR build of this version)$"
)
_SUGGEST_RESET_RE = re.compile(
    r"^(?:- |summary:|Submit in this order|=== stage|no review needed:|other specs)"
)
_SUGGEST_QUOTED_RE = re.compile(r"'([^']{1,120})'")
_SUGGEST_LINE_RE = re.compile(r"rust-deps(?: --root \S+)? ([a-z][a-z-]+)(.*)")


def _is_literal_value(token: str) -> bool:
    """An option value the tool interpolated, not a metavar placeholder (NAME, <chroot>)."""
    return bool(token) and not re.match(r'^(?:<.*>|".*|[A-Z][A-Z0-9_=|,.]*)$', token)


def tui_suggested_actions(
    lines: list[str], root: Path, commands: dict
) -> list[tuple[str, list[str], list[str], dict[str, str], str]]:
    """Steps the tools themselves propose in their own output, as jumpable actions.

    A proposal is a quoted command ('review', 'copr --wait', 'adopt crate') or a
    full 'rust-deps …' line.  Crates are the ones named in the line (resolved
    against the tree), else the package block the proposal sits in.  Literal
    option values are captured so the jump prefills them (--project, -r);
    metavar placeholders are skipped.  A command with a single positional crate
    (copr-log) gets it as a form value.  The tool's own wording is kept as the why.
    """
    crates_of: dict[str, str] = {}
    for p in packages.local_packages(root).values():
        crates_of[p.crate] = p.crate
        crates_of[p.rpm_name] = p.crate

    def args_of(
        cmd: str, tokens: list[str]
    ) -> tuple[list[str], list[str], dict[str, str]]:
        flags: list[str] = []
        picked: list[str] = []
        values: dict[str, str] = {}
        opts = {o: f for f in commands[cmd][1] for o in f.options}
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t in opts:
                f = opts[t]
                if f.kind == "flag":
                    flags.append(f.key)
                elif i + 1 < len(tokens) and _is_literal_value(tokens[i + 1]):
                    v = tokens[i + 1]
                    values[f.key] = (
                        f"{values[f.key]} {v}"
                        if f.kind == "multi" and f.key in values
                        else v
                    )
                    i += 1
                else:
                    i += 1  # skip the option's placeholder value
            elif t in crates_of:
                picked.append(crates_of[t])
            i += 1
        return flags, picked, values

    out: list[tuple[str, list[str], list[str], dict[str, str], str]] = []
    seen: set[tuple] = set()
    ctx: list[str] = []

    def add(
        cmd: str, crates: list[str], flags: list[str], values: dict[str, str], why: str
    ) -> None:
        pos = next(
            (f for f in commands[cmd][1] if f.positional and f.kind == "text"), None
        )
        if pos is not None and pos.dest != "crates" and crates:
            values = {**values, pos.key: crates[0]}
            crates = []
        key = (cmd, tuple(crates), tuple(flags), tuple(sorted(values.items())))
        if key not in seen:
            seen.add(key)
            out.append((cmd, crates, flags, values, why))

    for line in lines:
        if m := _SUGGEST_STEP_RE.match(line):
            head = m.group(1)
        elif m := _SUGGEST_BLOCK_RE.match(line):
            head = m.group(1)
        elif m := _SUGGEST_COPR_RE.match(line):
            head = m.group(1)
        elif _SUGGEST_RESET_RE.match(line):
            head, ctx = "", []
        else:
            head = ""
        if head:
            ctx = [crates_of[head]] if head in crates_of else []
        stripped = line.strip()
        if not stripped:
            continue
        for m in _SUGGEST_LINE_RE.finditer(line):
            if m.group(1) in commands:
                flags, picked, values = args_of(m.group(1), m.group(2).split())
                add(m.group(1), picked, flags, values, stripped)
        for m in _SUGGEST_QUOTED_RE.finditer(line):
            tokens = m.group(1).split()
            if tokens and tokens[0] in commands:
                flags, picked, values = args_of(tokens[0], tokens[1:])
                add(tokens[0], picked or list(ctx), flags, values, stripped)
    return out
