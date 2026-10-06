"""rust-deps TUI results: parse command output into tables and next steps."""

from __future__ import annotations

from pathlib import Path
import json
import re

from . import doctor
from . import packages
from . import reviewrequest
from . import tui_overview

_TRIAL_HEADER_RE = re.compile(r"^== trial (\S+) (\S+) \(log: (.*)\)$")
_TRIAL_STATUS_RE = re.compile(r"^   (\S.*?)\s{2,}(ok|FAILED|BROKEN|skipped)\s+(.*)$")


def parse_trial_output(lines: list[str]) -> list[dict]:
    """The 'trial' blocks as rows: crate, version, target, verdict, summary, first error.

    Each row also carries its block's mode: 'plain' runs the spec's invocations,
    'discover' tried every target and suggested a [tests] table, 'recheck' is the
    verification trial --apply chains after writing that table.
    """
    blocks: list[dict] = []
    cur: dict | None = None
    for line in lines:
        if m := _TRIAL_HEADER_RE.match(line):
            cur = {
                "crate": m.group(1),
                "version": m.group(2),
                "log": m.group(3),
                "rows": [],
                "suggested": False,
                "applied": False,
            }
            blocks.append(cur)
            continue
        if cur is None:
            continue
        if m := _TRIAL_STATUS_RE.match(line):
            cur["rows"].append(
                {
                    "target": m.group(1).strip(),
                    "status": m.group(2),
                    "summary": m.group(3).strip(),
                    "detail": "",
                }
            )
        elif line.startswith("      ") and cur["rows"]:
            if not cur["rows"][-1]["detail"]:
                cur["rows"][-1]["detail"] = line.strip()
        elif (
            "Suggested [tests] table" in line
            or "no [tests] restrictions needed" in line
        ):
            cur["suggested"] = True
        elif "Written to " in line and "regenerating the spec" in line:
            cur["applied"] = True
    rows: list[dict] = []
    for b in blocks:
        mode = (
            "recheck"
            if b["log"].endswith(".recheck.log")
            else "discover" if b["suggested"] else "plain"
        )
        for r in b["rows"]:
            rows.append(
                {
                    "crate": b["crate"],
                    "version": b["version"],
                    "log": b["log"],
                    "mode": mode,
                    "applied": b["applied"],
                    **r,
                }
            )
    return rows


def tui_trial_actions(
    rows: list[dict],
) -> list[tuple[str, list[str], list[str], dict[str, str], str]]:
    """Next steps implied by a trial run, plain or discovery.  The last block per
    crate decides: --apply chains a recheck after writing the suggested table."""
    last: dict[str, dict] = {}
    for r in rows:
        blk = last.setdefault(r["crate"], {"mode": "plain", "failed": [], "log": ""})
        if r["log"] != blk["log"]:
            blk = {"mode": r["mode"], "failed": [], "log": r["log"]}
            last[r["crate"]] = blk
        if r["status"] in ("FAILED", "BROKEN"):
            blk["failed"].append(r["target"])

    def group(pred) -> list[str]:
        return sorted(c for c, b in last.items() if pred(b))

    out: list[tuple[str, list[str], list[str], dict[str, str], str]] = []
    # a discovery that found failures has already produced the suggestion: apply it
    if s := group(lambda b: b["mode"] == "discover" and b["failed"]):
        out.append(
            (
                "trial",
                s,
                ["discover", "apply"],
                {},
                "a [tests] table was suggested: --apply writes it, regenerates and rechecks",
            )
        )
    for c in group(lambda b: b["mode"] == "plain" and b["failed"]):
        out.append(
            (
                "trial",
                [c],
                ["discover"],
                {},
                f"{', '.join(last[c]['failed'])} failed: --discover suggests the [tests] table that skips them",
            )
        )
    if s := group(lambda b: b["mode"] == "recheck" and b["failed"]):
        out.append(
            (
                "trial",
                s,
                ["discover"],
                {},
                "the applied table still fails: --discover suggests another one",
            )
        )
    if s := group(lambda b: b["mode"] == "recheck" and not b["failed"]):
        out.append(
            (
                "tests-edit",
                s,
                [],
                {},
                "the applied table passes: write the real reasons into rust2rpm.toml, then regen",
            )
        )
    if s := group(lambda b: not b["failed"] and b["mode"] != "recheck"):
        out.append(("srpm", s, [], {}, "all their test targets pass: build the SRPMs"))
    return out


_SRPM_HEADER_RE = re.compile(r"^== (\S+) (\S+)$")
_SRPM_RPMLINT_RE = re.compile(r"checked; (.*)$")


def parse_srpm_output(lines: list[str]) -> list[dict]:
    """The 'srpm' blocks as rows: crate, version, srpm, %prep, rpmlint.

    Each stage prints its result line only when it succeeds, so a missing line
    marks where the run stopped (its error text is in the log); with --no-prep
    the %prep line is absent, but a later line proves the run continued.
    """
    rows: list[dict] = []
    cur: dict | None = None
    for line in lines:
        if m := _SRPM_HEADER_RE.match(line):
            cur = {
                "crate": m.group(1),
                "version": m.group(2),
                "srpm": "",
                "prep": "",
                "rpmlint": "",
            }
            rows.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("Wrote: "):
            cur["srpm"] = s.removeprefix("Wrote: ").rsplit("/", 1)[-1]
        elif s == "%prep ok":
            cur["prep"] = "ok"
        elif m := _SRPM_RPMLINT_RE.search(s):
            cur["rpmlint"] = m.group(1)
        elif s == "rpmlint: no output":
            cur["rpmlint"] = "no output"
    for r in rows:
        r["srpm"] = r["srpm"] or "FAILED"
        r["prep"] = r["prep"] or ("skipped" if r["rpmlint"] else "FAILED")
        r["rpmlint"] = r["rpmlint"] or "FAILED"
    return rows


def split_columns(line: str) -> list[str]:
    """The cells of an aligned output line: columns are separated by 2+ spaces."""
    return [p for p in re.split(r"\s{2,}", line.strip()) if p]


_HEADER_CELL_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+/-]{0,31}")


def parse_columns_table(lines: list[str]) -> tuple[list[str], list[list[str]]] | None:
    """The 'status' format: a header row of short tokens followed by rows with the
    same column count."""
    for i, line in enumerate(lines):
        cells = split_columns(line)
        if len(cells) < 3 or not all(_HEADER_CELL_RE.fullmatch(c) for c in cells):
            continue
        rows: list[list[str]] = []
        for following in lines[i + 1 :]:
            more = split_columns(following)
            if len(more) != len(cells):
                break
            rows.append(more)
        if rows:
            return cells, rows
    return None


_STATE_LINE_RE = re.compile(r"^\s{3}(ok|absent|MISSING|WARNING|root)\s+(\S+)?\s*(.*)$")


def parse_state_table(lines: list[str]) -> tuple[list[str], list[list[str]]] | None:
    """The 'doctor' format: a state word, a tool or check name, and a detail."""
    rows: list[list[str]] = []
    for line in lines:
        m = _STATE_LINE_RE.match(line)
        if not m:
            return None
        state, name, detail = m.groups()
        rows.append([state, name or "", detail])
    if len(rows) < 3:
        return None
    return ["state", "tool", "detail"], rows


def parse_json_table(data: object) -> tuple[list[str], list[list[str]]] | None:
    """Render a '--json' result as a table: arrays of arrays as build stages,
    arrays of objects by their keys, an object as key/value."""
    if not data:
        return None

    def cell(v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, (str, int, float, bool)):
            return str(v)
        return json.dumps(v, separators=(",", " "))

    if isinstance(data, list) and data and all(isinstance(x, list) for x in data):
        return ["stage", "packages"], [
            [str(i + 1), " ".join(cell(x) for x in row)] for i, row in enumerate(data)
        ]
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        columns = list(dict.fromkeys(k for row in data for k in row))
        return columns, [[cell(row.get(c)) for c in columns] for row in data]
    if isinstance(data, dict):
        return ["key", "value"], [[k, cell(v)] for k, v in data.items()]
    if isinstance(data, list):
        return ["value"], [[cell(v) for v in data]]
    return None


def render_trial_output(lines: list[str], root: Path):
    """trial's verdict lines are the case where the log hides the shape:
    render them as a table and turn failures into next steps."""
    trial_rows = parse_trial_output(lines)
    if not trial_rows:
        return None, []
    parsed = (
        ["crate", "version", "target", "status", "summary", "first error"],
        [
            [
                r["crate"],
                r["version"],
                r["target"],
                r["status"],
                r["summary"],
                r["detail"],
            ]
            for r in trial_rows
        ],
    )
    return parsed, tui_trial_actions(trial_rows)


def render_srpm_output(lines: list[str], root: Path):
    """like trial, srpm's per-crate stage lines are verdict-shaped"""
    srpm_rows = parse_srpm_output(lines)
    if not srpm_rows:
        return None, []
    parsed = (
        ["crate", "version", "srpm", "%prep", "rpmlint"],
        [
            [r["crate"], r["version"], r["srpm"], r["prep"], r["rpmlint"]]
            for r in srpm_rows
        ],
    )
    return parsed, []


def render_status_output(lines: list[str], root: Path):
    return parse_columns_table(lines), []


def render_doctor_output(lines: list[str], root: Path):
    return parse_state_table(lines), []


_CS_STAGE_RE = re.compile(r"^=== stage (\d+)$")
_CS_PKG_RE = re.compile(r"^(\S+) (\S+): (?:no COPR build of this version|build .*)$")
_CS_VERDICT_RE = re.compile(r"^   (ok|BUILDING|WAIT|RETRY|BLOCKED|FAILED)\s+(.*)$")


def parse_copr_status_output(lines: list[str]) -> list[dict]:
    """The 'copr-status' diagnosis as ladder rows: stage, crate, evr, verdict, chroots.

    Chroots grouped under one verdict share a row; the '(build N)' reference to
    an older build and the diagnostic notes stay in the log."""
    rows: list[dict] = []
    stage, cur = 0, None
    for line in lines:
        if m := _CS_STAGE_RE.match(line):
            stage, cur = int(m.group(1)), None
        elif m := _CS_PKG_RE.match(line):
            if "no COPR build" in line:
                rows.append(
                    {
                        "stage": stage,
                        "crate": m.group(1),
                        "evr": m.group(2),
                        "verdict": "none",
                        "chroots": "",
                    }
                )
                cur = None
            else:
                cur = {"stage": stage, "crate": m.group(1), "evr": m.group(2)}
        elif cur is not None and (m := _CS_VERDICT_RE.match(line)):
            chroots = " ".join(
                t for t in m.group(2).split() if not t.startswith(("(", "http"))
            )
            rows.append({**cur, "verdict": m.group(1), "chroots": chroots})
        elif line.startswith(("summary:", "- ")):
            cur = None
    return rows


def render_copr_status_output(lines: list[str], root: Path):
    rows = parse_copr_status_output(lines)
    if not rows:
        return None, []
    parsed = (
        ["stage", "crate", "evr", "verdict", "chroots"],
        [
            [str(r["stage"]), r["crate"], r["evr"], r["verdict"], r["chroots"]]
            for r in rows
        ],
    )
    return parsed, []


_PLAN_STAGE_RE = re.compile(r"^=== stage (\d+)$")
_PLAN_AFTER_RE = re.compile(r"^       after: (.*)$")


def parse_review_plan_output(lines: list[str]) -> list[dict]:
    """The review-plan steps as board rows: stage, package, version, state, detail, after.

    The numbered step lines carry the verdict; the indented lines below a step
    are its detail and the packages its ticket must wait for."""
    rows: list[dict] = []
    stage, cur = 0, None
    for line in lines:
        if m := _PLAN_STAGE_RE.match(line):
            stage, cur = int(m.group(1)), None
        elif m := tui_overview._SUGGEST_STEP_RE.match(line):
            cur = {
                "stage": stage,
                "package": m.group(1),
                "version": m.group(2),
                "state": m.group(3),
                "detail": "",
                "after": "",
            }
            rows.append(cur)
        elif cur is not None and line.startswith("       "):
            if m := _PLAN_AFTER_RE.match(line):
                cur["after"] = m.group(1)
            else:
                cur["detail"] = line.strip()
        elif tui_overview._SUGGEST_RESET_RE.match(line):
            cur = None
    return rows


def render_review_plan_output(lines: list[str], root: Path):
    rows = parse_review_plan_output(lines)
    if not rows:
        return None, []
    parsed = (
        ["stage", "package", "version", "state", "detail", "after"],
        [
            [
                str(r["stage"]),
                r["package"],
                r["version"],
                r["state"],
                r["detail"],
                r["after"],
            ]
            for r in rows
        ],
    )
    # the tool's own 'file the READY ones' line becomes the filing jump
    return parsed, []


_RR_HEADER_RE = re.compile(r"^== review request (\S+) (\S+)$")


def parse_review_request_drafts(lines: list[str], root: Path) -> list[dict]:
    """The drafts a review-request run wrote, read back from the tree: the filing gate
    shows the Summary and the URLs a Bugzilla ticket would carry."""
    by_name = {p.rpm_name: p for p in packages.local_packages(root).values()}
    rows: list[dict] = []
    for line in lines:
        m = _RR_HEADER_RE.match(line)
        if not m:
            continue
        pkg = by_name.get(m.group(1))
        draft = pkg.dir / reviewrequest.REQUEST_DRAFT if pkg is not None else None
        if draft is None or not draft.exists():
            continue
        text = draft.read_text()

        def field(name: str) -> str:
            fm = re.search(rf"^{name}: (.*)$", text, re.M)
            return fm.group(1) if fm else ""

        rows.append(
            {
                "crate": pkg.crate,
                "version": m.group(2),
                "summary": field("Summary"),
                "spec": field("Spec URL"),
                "srpm": field("SRPM URL"),
                "filed": (pkg.dir / reviewrequest.REQUEST_STATE).exists(),
            }
        )
    return rows


def render_review_request_output(lines: list[str], root: Path):
    rows = parse_review_request_drafts(lines, root)
    if not rows:
        return None, []
    parsed = (
        ["crate", "version", "summary", "spec", "srpm"],
        [[r["crate"], r["version"], r["summary"], r["spec"], r["srpm"]] for r in rows],
    )
    actions = []
    unfiled = [r["crate"] for r in rows if not r["filed"]]
    if unfiled:
        actions.append(
            (
                "review-request",
                unfiled,
                ["file"],
                {},
                "the table is what filing would post: file these drafts",
            )
        )
    return parsed, actions


_RESOLVE_ROW_RE = re.compile(
    r"^(NEW|UPDATE|FEATURES)\s+(\S+) (\S+)(?: \(Fedora has [^)]*\))?  <- (.*)$"
)


def parse_resolve_output(lines: list[str]) -> list[dict]:
    rows: list[dict] = []
    for line in lines:
        if m := _RESOLVE_ROW_RE.match(line):
            rows.append(
                {
                    "status": m.group(1),
                    "crate": m.group(2),
                    "version": m.group(3),
                    "needed_by": m.group(4),
                }
            )
    return rows


def render_resolve_output(lines: list[str], root: Path):
    rows = parse_resolve_output(lines)
    if not rows:
        return None, []
    parsed = (
        ["status", "crate", "version", "needed by"],
        [[r["status"], r["crate"], r["version"], r["needed_by"]] for r in rows],
    )
    actions = []
    new = [r["crate"] for r in rows if r["status"] == "NEW"]
    if new:
        # the names are not packages yet: they arrive in init's 'other crates' field,
        # and the target chroot carries over from this resolve run
        actions.append(
            (
                "init",
                new,
                ["recursive"],
                {},
                "not in Fedora or the tree: init --recursive creates the packages",
            )
        )
    return parsed, actions


_ORDER_STAGE_RE = re.compile(r"^stage (\d+): (.*)$")


_WORKSPACE_ROW_RE = re.compile(
    r"^(SYSTEM|PACKAGED|FEATURES|UPDATE|NEW|WORKSPACE|PATH|GIT|ALTREG|OPTIONAL)\s+"
    # the version is optional: the '<-' that opens the needed-by list must not
    # be taken for it, or a row without a version loses its needed_by
    r"(\S+)(?:\s+(?!req\b|<-)(\S+))?(.*?)$"
)


def parse_workspace_output(lines: list[str]) -> list[dict]:
    """'workspace' rows: every crate the project needs, and what provides it."""
    rows: list[dict] = []
    for line in lines:
        if not (m := _WORKSPACE_ROW_RE.match(line)):
            continue
        asked, _, needed_by = (m.group(4) or "").partition(" <- ")
        rows.append(
            {
                "status": m.group(1),
                "crate": m.group(2),
                "version": m.group(3) or "",
                "asked": asked.strip().removeprefix("req").strip(),
                "needed_by": needed_by.strip(),
            }
        )
    return rows


def render_workspace_output(lines: list[str], root: Path):
    rows = parse_workspace_output(lines)
    if not rows:
        return None, []
    parsed = (
        ["status", "crate", "version", "asks for", "needed by"],
        [
            [r["status"], r["crate"], r["version"], r["asked"], r["needed_by"]]
            for r in rows
        ],
    )
    actions = []
    missing = [r["crate"] for r in rows if r["status"] == "NEW"]
    if missing:
        actions.append(
            (
                "init",
                missing,
                ["recursive"],
                {},
                "Fedora has none of these: init --recursive packages them for the project",
            )
        )
    return parsed, actions


def render_order_output(lines: list[str], root: Path):
    """'order' prints one 'stage N: crates' line per build stage: show the ladder."""
    rows = [
        [m.group(1), m.group(2)] for line in lines if (m := _ORDER_STAGE_RE.match(line))
    ]
    if not rows:
        return None, []
    parsed = (["stage", "packages"], rows)
    return parsed, []


def parse_tool_states(lines: list[str]) -> dict[str, bool]:
    """doctor's tool lines as {tool: available}; anything else is ignored.

    Only the tool's own presence line counts: the 'mock group' warning is a
    setup hint, not a missing tool (doctor does not fail on it either)."""
    out: dict[str, bool] = {}
    parsed = parse_state_table(lines)
    if not parsed:
        return out
    for state, name, _ in parsed[1]:
        if name in doctor.REQUIRED_TOOLS or name in doctor.OPTIONAL_TOOLS:
            out.setdefault(name, state == "ok")
    return out


_UPDATE_PLAN_RE = re.compile(r"^Updates, in build order: (.*)$")
_UPDATE_PAIR_RE = re.compile(r"^(\S+) (\S+) -> (\S+)$")
_UPDATE_DONE_RE = re.compile(r"^== (\S+) (\S+) -> (\S+)$")
_UPDATE_BLOCKED_RE = re.compile(r"^== (\S+): (.*)$")


def parse_update_output(lines: list[str]) -> list[dict]:
    """The update decision as rows: what a dry run plans, what a run did, what is blocked."""
    by: dict[str, dict] = {}
    for line in lines:
        if m := _UPDATE_PLAN_RE.match(line):
            for item in m.group(1).split(", "):
                if pm := _UPDATE_PAIR_RE.match(item):
                    by[pm.group(1)] = {
                        "crate": pm.group(1),
                        "from": pm.group(2),
                        "to": pm.group(3),
                        "note": "",
                    }
        elif m := _UPDATE_DONE_RE.match(line):
            by[m.group(1)] = {
                "crate": m.group(1),
                "from": m.group(2),
                "to": m.group(3),
                "note": "updated",
            }
        elif m := _UPDATE_BLOCKED_RE.match(line):
            by.setdefault(
                m.group(1),
                {"crate": m.group(1), "from": "-", "to": "-", "note": m.group(2)},
            )
    return list(by.values())


def render_update_output(lines: list[str], root: Path):
    rows = parse_update_output(lines)
    if not rows:
        return None, []
    parsed = (
        ["crate", "current", "target", "note"],
        [[r["crate"], r["from"], r["to"], r["note"]] for r in rows],
    )
    actions = []
    planned = [r["crate"] for r in rows if r["note"] == ""]
    if planned:
        actions.append(
            (
                "update",
                planned,
                [],
                {},
                "the dry run listed these updates in build order: apply them",
            )
        )
    return parsed, actions
