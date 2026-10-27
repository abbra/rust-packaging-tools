"""rust-deps TUI actions: the verbs a packager picks, and the settings they share."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import json

from . import config
from . import doctor
from . import packages
from . import tui_form
from . import tui_overview
from . import tui_playbooks


@dataclass
class TuiAction:
    """One thing a packager does, named for what it achieves: 'cmd' is the
    rust-deps subcommand it runs ('' for a screen of the TUI itself), 'flags'
    the switches that make it this action (trial --discover is "Choose tests"),
    'preset' switches that start checked but can be turned off, 'inputs' the
    fields shown up front (the rest waits under More options), 'labels' their
    names in words instead of option strings."""

    key: str
    verb: str
    cmd: str
    explain: str
    flags: tuple[str, ...] = ()
    preset: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)


# Words for the fields that recur across commands; an action's own 'labels' win.
FIELD_LABELS = {
    "crates": "Packages",
    "crate": "Package",
    "all": "All packages under the root",
    "project": "COPR project",
    "chroot": "Chroot",
    "fas": "FAS account",
    "dir": "Directory for dist-git checkouts",
    "projects": "Project directory or Cargo.toml",
    "local_root": "Directory with local crates",
    "manifest": "Cargo.toml of the project",
    "version": "Version",
    "dry_run": "Dry run: only show what would happen",
    "wait": "Wait for the builds and diagnose failures",
}

_ACTIONS = [
    TuiAction(
        "regen",
        "Regenerate spec",
        "regen",
        "Run rust2rpm again: the spec and Cargo.toml patch from rust2rpm.toml"
        " and cargo-toml-edits.toml.",
    ),
    TuiAction(
        "update",
        "Update to a newer version",
        "update",
        "Move to a new upstream release, in build order, then regenerate and test.",
        inputs=("version", "dry_run"),
        labels={"version": "Version (empty: the newest)"},
    ),
    TuiAction(
        "choose-tests",
        "Choose tests",
        "trial",
        "Try every test target offline, as mock would, and suggest a [tests]"
        " table that skips the ones that cannot pass.",
        flags=("discover",),
    ),
    TuiAction(
        "apply-tests",
        "Write the [tests] table",
        "trial",
        "Write the suggested [tests] table into rust2rpm.toml, regenerate the"
        " spec and run the tests again.",
        flags=("discover", "apply"),
    ),
    TuiAction(
        "run-tests",
        "Run tests",
        "trial",
        "Run exactly the tests the spec's %check runs.",
    ),
    TuiAction(
        "explain-skips",
        "Explain test skips",
        "",
        "Replace the TODO comments of the [tests] table with the real reasons.",
    ),
    TuiAction(
        "srpm",
        "Build SRPM",
        "srpm",
        "Download the sources, build the source RPM, check %prep and rpmlint.",
    ),
    TuiAction(
        "order",
        "Show build order",
        "order",
        "Which packages build first: each stage needs only earlier ones and Fedora.",
    ),
    TuiAction(
        "mock-chain",
        "Build locally in mock",
        "mock-chain",
        "Build the SRPMs in dependency order with mock --chain: the local,"
        " authoritative test.",
        inputs=("chroot",),
    ),
    TuiAction(
        "check-targets",
        "Check dependencies in targets",
        "check-targets",
        "Would every BuildRequires resolve in each target release?  No build needed.",
        inputs=("project", "chroot"),
        labels={"chroot": "Chroots"},
    ),
    TuiAction(
        "copr",
        "Publish to COPR",
        "copr",
        "Submit the SRPMs to the COPR project, one batch per build stage.",
        preset=("wait",),
        inputs=("project", "chroot", "wait"),
        labels={"chroot": "Chroots (empty: all of the project's)"},
    ),
    TuiAction(
        "copr-status",
        "Check COPR builds",
        "copr-status",
        "Each chroot's result, and for failures the reason and what to do.",
        inputs=("project", "chroot"),
        labels={"chroot": "Chroots (empty: all)"},
    ),
    TuiAction(
        "copr-log",
        "Read a COPR build log",
        "copr-log",
        "Fetch a build log and summarize its errors.",
        inputs=("chroot", "project"),
    ),
    TuiAction(
        "tmt",
        "Run tmt tests",
        "tmt",
        "Run the generated tmt tests in a container, as Fedora CI will.",
        inputs=("project", "chroot"),
    ),
    TuiAction(
        "review",
        "Run fedora-review",
        "review",
        "Rebuild in mock and run fedora-review; run 'Build locally in mock' first.",
        inputs=("chroot",),
    ),
    TuiAction(
        "review-plan",
        "Plan review submissions",
        "review-plan",
        "What to submit to Fedora, in which order, and what is still missing.",
        inputs=("project",),
    ),
    TuiAction(
        "review-request",
        "Draft review request",
        "review-request",
        "Write the Bugzilla review request from the latest COPR build.",
        inputs=("project", "fas"),
    ),
    TuiAction(
        "file-request",
        "File review request",
        "review-request",
        "File the drafted review request on Bugzilla.",
        flags=("file",),
        inputs=("project", "fas"),
    ),
    TuiAction(
        "review-status",
        "Check review tickets",
        "review-status",
        "Where each review ticket stands, new comments, and the next step.",
    ),
    TuiAction(
        "request-repo",
        "Request the dist-git repository",
        "request-repo",
        "Ask Fedora for the repository of an approved package, referencing its "
        "review ticket; needs a Forgejo token ('fedpkg set-forgejo-token').",
        inputs=("dry_run",),
    ),
    TuiAction(
        "adopt",
        "Adopt Fedora's packaging",
        "adopt",
        "Take over the dist-git rust2rpm.toml and patches of a package Fedora has.",
    ),
    TuiAction(
        "dist-git",
        "Put packaging in dist-git",
        "dist-git",
        "For an adopted package: commit the update on a branch of its dist-git "
        "checkout. For a repository your review request created: clone it and "
        "commit the packaging on rawhide.",
        inputs=("dir", "dry_run"),
    ),
    TuiAction(
        "status",
        "Compare with Fedora releases",
        "status",
        "The packaged version against crates.io and the Fedora releases.",
    ),
    TuiAction(
        "resolve",
        "Find missing crates",
        "resolve",
        "Which crates (and their dependencies) Fedora does not have yet.",
        inputs=("manifest", "local_root"),
        labels={"crates": "Crates (name or name@requirement)"},
    ),
    TuiAction(
        "workspace",
        "Audit a Rust project",
        "workspace",
        "Where every crate the project needs comes from, and what Fedora lacks.",
        inputs=("local_root",),
    ),
    TuiAction(
        "init",
        "Create packages",
        "init",
        "Create the packages and every missing dependency, then choose their tests.",
        flags=("recursive",),
        labels={"crates": "Crates (name or name@requirement)"},
    ),
    TuiAction(
        "doctor",
        "Check setup",
        "doctor",
        "Are the tools installed, and can mock and the tests run here?",
    ),
]
ACTIONS = {a.key: a for a in _ACTIONS}

# What 'other actions' offers per stage, the stage's own action first.
STAGE_ACTIONS = {
    "spec?": ["regen", "update"],
    "tests?": ["choose-tests", "run-tests", "regen", "update"],
    "tests-todo": ["explain-skips", "run-tests", "regen"],
    "srpm?": ["srpm", "run-tests", "regen", "update"],
    "review": [
        "review-plan",
        "mock-chain",
        "check-targets",
        "copr",
        "copr-status",
        "review",
        "review-request",
        "order",
        "status",
        "update",
        "regen",
    ],
    "draft": ["file-request", "review-request", "review-plan", "copr-status"],
    "filed": ["review-status", "request-repo", "copr", "copr-status", "update"],
    "requested": ["dist-git", "request-repo", "review-status", "copr", "copr-status", "update"],
    "adopted": ["dist-git", "adopt", "copr", "copr-status", "status", "update"],
    "targets": ["copr", "copr-status", "check-targets", "tmt", "status", "update"],
}


def action_for(cmd: str, flags: list[str] | tuple[str, ...] = ()) -> TuiAction:
    """The action a command line is: the one whose identifying flags it carries,
    most specific first.  'tests-edit' is the TUI's own reason editor."""
    if cmd == "tests-edit":
        return ACTIONS["explain-skips"]
    have = set(flags)
    matches = [a for a in _ACTIONS if a.cmd == cmd and set(a.flags) <= have]
    if matches:
        return max(matches, key=lambda a: len(a.flags))
    return TuiAction(cmd, f"Run {cmd}", cmd, "")


def field_label(action: TuiAction, f: tui_form.TuiField) -> str:
    return action.labels.get(f.key) or FIELD_LABELS.get(f.key) or f.label


def missing_tools(cmd: str, tools_ok: dict[str, bool]) -> list[tuple[str, str]]:
    """Tools the command needs that 'doctor' found missing, with what to install."""
    pb = tui_playbooks.TUI_PLAYBOOKS.get(cmd, tui_playbooks.TuiPlaybook())
    return [
        (t, doctor.OPTIONAL_TOOLS.get(t, t).split(" (")[0])
        for t in pb.requires
        if tools_ok.get(t) is False
    ]


@dataclass
class TuiSettings:
    """What every action of one package root shares, asked once: the COPR
    project, the chroots to build for, the Bugzilla (FAS) account, where
    dist-git checkouts live."""

    project: str = ""
    chroots: list[str] = field(default_factory=list)
    fas: str = ""
    dist_git_dir: str = ""


SETTINGS_FILE = "tui-settings.json"  # in config.CACHE_DIR, keyed by package root


def _settings_path() -> Path:
    return config.CACHE_DIR / SETTINGS_FILE


def load_settings(root: Path) -> TuiSettings:
    try:
        data = json.loads(_settings_path().read_text()).get(str(root.resolve()), {})
    except (OSError, ValueError, AttributeError):
        data = {}
    known = TuiSettings.__dataclass_fields__
    return TuiSettings(**{k: v for k, v in data.items() if k in known})


def save_settings(root: Path, settings: TuiSettings) -> None:
    path = _settings_path()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[str(root.resolve())] = asdict(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def setting_value(
    settings: TuiSettings, f: tui_form.TuiField
) -> str | list[str] | None:
    """What the settings say a field should hold, or None when they do not cover it.
    A repeatable chroot takes every chroot, a single one the first."""
    if f.positional:
        return None
    if f.key == "project":
        return settings.project or None
    if f.key == "fas":
        return settings.fas or None
    if f.key == "dir":
        return settings.dist_git_dir or None
    if f.key == "chroot" and settings.chroots:
        return list(settings.chroots) if f.kind == "multi" else settings.chroots[0]
    return None


def remember(settings: TuiSettings, values: dict) -> bool:
    """Keep what a run was given for a setting that was still empty, so the next
    action does not ask again.  Returns whether anything changed."""
    changed = False
    for key, attr in (("project", "project"), ("fas", "fas"), ("dir", "dist_git_dir")):
        v = values.get(key)
        if isinstance(v, str) and v.strip() and not getattr(settings, attr):
            setattr(settings, attr, v.strip())
            changed = True
    chroot = values.get("chroot")
    if chroot and not settings.chroots:
        settings.chroots = list(chroot) if isinstance(chroot, list) else chroot.split()
        changed = True
    return changed


def initial_values(
    action: TuiAction,
    fields: list[tui_form.TuiField],
    crates: list[str],
    settings: TuiSettings,
    flags: list[str] | tuple[str, ...] = (),
    values: dict[str, str] | None = None,
) -> dict:
    """The form an action opens with: its packages, its flags, the settings, then
    the values a suggestion named (those win: the tool asked for exactly them)."""
    out: dict = {}
    on = set(action.flags) | set(action.preset) | set(flags)
    for f in fields:
        if f.positional and f.dest == "crates":
            out[f.key] = " ".join(crates)
        elif f.positional and f.kind == "text" and crates:
            out[f.key] = crates[0]  # copr-log: one package
        elif f.kind == "flag":
            out[f.key] = f.key in on
        elif (v := setting_value(settings, f)) is not None:
            out[f.key] = v
        elif f.default:
            out[f.key] = f.default
    for k, v in (values or {}).items():
        f = next((fl for fl in fields if fl.key == k), None)
        out[k] = v.split() if f is not None and f.kind == "multi" else v
    return out


def primary_keys(action: TuiAction, fields: list[tui_form.TuiField]) -> list[str]:
    """The fields shown up front: what the command acts on, the action's own
    inputs, and anything it cannot run without."""
    keys = [f.key for f in fields if f.positional]
    keys += [k for k in action.inputs if any(f.key == k for f in fields)]
    keys += [f.key for f in fields if f.required and f.key not in keys]
    if any(f.key == "all" for f in fields):
        keys.insert(1, "all")
    return list(dict.fromkeys(keys))


def action_step(key: str, crates: list[str], why: str) -> tuple:
    """An action as a next-step tuple (cmd, crates, flags, values, why)."""
    a = ACTIONS[key]
    return (a.cmd or "tests-edit", crates, list(a.flags), {}, why)


def follow_ups(root: Path, crates: list[str], after: str = "") -> list[tuple]:
    """After a run, what comes next for the packages it touched, from where they
    now sit on the lifecycle graph: one step per stage they reached.  A stage
    whose action is the one that just ran ('after') offers nothing: running it
    again would not move the packages on."""
    pkgs = packages.local_packages(root)
    at: dict[str, list[str]] = {}
    for c in crates:
        if c in pkgs:
            at.setdefault(tui_overview.tui_stage(pkgs[c]).name, []).append(c)
    return [
        action_step(
            tui_overview.STAGES[name].action,
            sorted(at[name]),
            f"now at '{tui_overview.STAGES[name].label}': {tui_overview.STAGES[name].blurb}",
        )
        for name in tui_overview.BOARD_ORDER
        if name in at and tui_overview.STAGES[name].action != after
    ]


def merge_steps(*groups: list[tuple]) -> list[tuple]:
    """Next steps from several sources, in order, without repeating one."""
    seen: set = set()
    out = []
    for group in groups:
        for step in group:
            cmd, crates, flags, values, _ = step
            key = (
                cmd,
                tuple(sorted(crates)),
                tuple(sorted(flags)),
                tuple(sorted(values.items())),
            )
            if key not in seen:
                seen.add(key)
                out.append(step)
    return out


def action_needed(lines: list[str]) -> list[str]:
    """The problems a run left for the packager: its 'ACTION NEEDED:' lines."""
    return [
        line.removeprefix("ACTION NEEDED:").strip()
        for line in lines
        if line.startswith("ACTION NEEDED:")
    ]


def is_project_path(text: str) -> bool:
    """Whether onboarding input names a project on disk rather than crates."""
    t = text.strip()
    return bool(t) and " " not in t and Path(tui_form.expand_user_path(t)).exists()
