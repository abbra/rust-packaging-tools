"""rust-deps TUI application: the Textual app itself."""

from __future__ import annotations

from pathlib import Path
import argparse
import os
import re
import shlex
import subprocess
import sys
import textwrap

from . import cli
from . import config
from . import packages
from . import tui_actions
from . import tui_form
from . import tui_overview
from . import tui_playbooks
from . import tui_results
from . import util

# 'rust-deps tui' guides a packager through the package lifecycle; it does not
# list the CLI.  Its home is a board: the stages of the lifecycle graph
# (tui_overview.TUI_STAGES) in pipeline order, each with the packages it holds
# and one plain-language action that moves them on (tui_actions).  Picking
# packages and pressing enter opens that action's sheet: the few inputs that
# matter, filled from the settings asked once per root (COPR project, chroots,
# FAS account), the rest folded under "More options", and the exact
# 'rust-deps …' line it will run.  A run streams its log, then ends in a
# summary: the verdict, the problems it left (ACTION NEEDED), its result as a
# table (tui_playbooks), and next steps — the ones the tool's output proposes
# and the ones the packages' new stages imply.  Every command stays reachable
# through "All commands", whose sheets are generated from the same argparse
# parser as the command line, so the UI cannot drift from it.  Each command
# runs as a 'rust-deps' subprocess.  The Textual APIs used here exist in
# Textual 4 (Fedora 44) and Textual 8.

_TUI_LINE_STYLES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^ERROR:"), "bold red"),
    (re.compile(r"^ACTION NEEDED:"), "bold magenta"),
    (re.compile(r"^WARNING:"), "yellow"),
    (re.compile(r"^=+ "), "bold cyan"),  # '== package' and '=== stage' blocks
    (re.compile(r"^\$ "), "dim"),  # echoed tool commands
    (re.compile(r"^(NEW|UPDATE|FEATURES)(?=\s)"), "cyan"),  # resolve verdicts
    (
        re.compile(r"^(SYSTEM|PACKAGED|WORKSPACE)(?=\s)"),
        "cyan",
    ),  # where a workspace crate comes from
    (re.compile(r"^(DROP|KEEP|OPTIONAL)(?=\s)"), "yellow"),  # the vendor audit
    (re.compile(r"^\s*(ok|root)(?=\s|$)"), "green"),
    (re.compile(r"^\s*(MISSING|FAILED)(?=\s|$)"), "bold red"),
    (re.compile(r"^\s*(BUILDING|WAIT|RETRY|BLOCKED|absent|WARNING)(?=\s|$)"), "yellow"),
    (re.compile(r"^\s*\[!\]"), "bold red"),  # unexplained review issues
    (re.compile(r"^\s*\[~\]"), "dim yellow"),  # expected review issues
    (re.compile(r"^(summary:|next:|stage \d+:)"), "bold"),
]


def tui_line_style(line: str) -> str:
    """The color of an output line, by the diagnostic prefix it starts with."""
    for pattern, style in _TUI_LINE_STYLES:
        if pattern.search(line):
            return style
    return ""


def crates_text(crates: list[str], limit: int = 3) -> str:
    """A package selection in a line: the first few names, then how many more."""
    if len(crates) <= limit:
        return " ".join(crates)
    return " ".join(crates[:limit]) + f" and {len(crates) - limit} more"


# Textual shrinks a container whose height is 'auto' inside a scrolling form to
# whatever is left, so every row a form mounts states its height (an Input with
# its border is three rows).
FORM_ROW_HEIGHT = 3


def make_tui_app() -> type:
    """The Textual app class.  Imports textual, so only 'tui' needs it."""
    import threading
    from rich.text import Text
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen, Screen
    from textual.widgets import (
        Button,
        Checkbox,
        Collapsible,
        ContentSwitcher,
        DataTable,
        Footer,
        Input,
        Label,
        OptionList,
        RichLog,
        Select,
        SelectionList,
        Static,
    )
    from textual.widgets.option_list import Option
    from textual.widgets.selection_list import Selection

    parser = cli.build_parser()
    subparsers = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    briefs = {action.dest: action.help or "" for action in subparsers._choices_actions}
    commands = {
        name: (
            briefs.get(name, ""),
            tui_form.tui_fields(sub),
            tui_form.tui_controls(sub),
        )
        for name, sub in subparsers.choices.items()
        if name != "tui"
    }

    def step_prompt(step: tuple) -> Text:
        """A next step as one line: the verb in bold, its packages, why (dim)."""
        cmd, crates, flags, values, why = step
        action = tui_actions.action_for(cmd, flags)
        prompt = Text()
        prompt.append(action.verb, style="bold")
        if crates:
            prompt.append("  " + crates_text(crates))
        extra = [v for k, v in values.items() if k not in ("crate",)]
        if values.get("crate"):
            prompt.append("  " + values["crate"])
        if extra:
            prompt.append("  (" + " ".join(extra) + ")", style="dim")
        if why:
            prompt.append("\n  " + why, style="dim")
        return prompt

    # ------------------------------------------------------------------ board

    class PackageList(SelectionList):
        """The packages of a stage: space picks, enter runs the stage's action."""

        BINDINGS = [Binding("enter", "primary", "Next step", show=False)]

        def action_primary(self) -> None:
            self.screen.action_primary()

    class BoardScreen(Screen):
        """Home: the lifecycle stages with their packages and the next step."""

        BINDINGS = [
            Binding("d", "details", "Details"),
            Binding("o", "other", "Other actions"),
            Binding("a", "toggle_all", "Select all"),
            Binding("slash", "filter", "Filter"),
            Binding("n", "new", "Package new"),
            Binding("s", "settings", "Settings"),
            Binding("question_mark", "commands", "All commands"),
            Binding("r", "refresh", "Refresh", show=False),
            Binding("q", "app.quit", "Quit"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.stage = ""  # '' is "All packages"
            self.groups: dict[str, list[packages.LocalPackage]] = {}

        def compose(self) -> ComposeResult:
            yield Static("", id="topbar")
            with Horizontal(id="board"):
                yield OptionList(id="stages")
                with Vertical(id="stage-pane"):
                    yield Static("", id="stage-title")
                    yield Static("", id="stage-blurb")
                    yield Input(placeholder="filter packages", id="filter")
                    yield PackageList(id="pkgs")
                    yield Static("", id="hint")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#filter").display = False
            self.refresh_board(first=True)

        def on_screen_resume(self) -> None:
            self.refresh_board()

        def refresh_board(self, first: bool = False) -> None:
            app = self.app
            self.groups = tui_overview.board(app.root)
            total = sum(len(v) for v in self.groups.values())
            stages = self.query_one("#stages", OptionList)
            keep = stages.highlighted
            stages.clear_options()
            stages.add_option(
                Option(self._stage_prompt("All packages", total), id="all")
            )
            for name in tui_overview.BOARD_ORDER:
                n = len(self.groups[name])
                stages.add_option(
                    Option(
                        self._stage_prompt(tui_overview.STAGES[name].label, n),
                        id=f"stage:{name}",
                    )
                )
            stages.add_option(Option(Text("─" * 26, style="dim"), disabled=True))
            stages.add_option(Option("+ Package something new", id="new"))
            stages.add_option(Option("⚙ Settings", id="settings"))
            stages.add_option(Option("? All commands", id="commands"))
            if first or keep is None:
                # open on the first stage with work in it; on nothing, on onboarding
                ids = [f"stage:{n}" for n in tui_overview.BOARD_ORDER if self.groups[n]]
                target = ids[0] if ids else "new"
                keep = next(
                    i
                    for i in range(stages.option_count)
                    if stages.get_option_at_index(i).id == target
                )
            stages.highlighted = keep
            self.update_topbar()
            self.show_stage()
            if first:
                (self.query_one("#pkgs") if total else stages).focus()

        @staticmethod
        def _stage_prompt(label: str, n: int) -> Text:
            t = Text(f"{label:<20}{n:>5}")
            if not n:
                t.stylize("dim")
            return t

        def update_topbar(self) -> None:
            app = self.app
            s = app.settings
            n = sum(len(v) for v in self.groups.values())
            root = str(app.root)
            home = str(Path.home())
            if root.startswith(home + "/"):
                root = "~" + root[len(home) :]
            if len(root) > 40:
                root = "…" + root[-39:]
            bar = Text(no_wrap=True, overflow="ellipsis")
            bar.append("rust-deps ", style="bold")
            bar.append(f"{n} packages in {root}")
            bar.append("   COPR: ", style="dim")
            bar.append(s.project or "not set")
            bar.append("   chroots: ", style="dim")
            bar.append(" ".join(s.chroots) or "not set")
            missing = [t for t, ok in app.tools_ok.items() if not ok]
            if missing:
                bar.append(
                    f"   ⚠ missing: {', '.join(missing)} (? → doctor)", style="yellow"
                )
            self.query_one("#topbar", Static).update(bar)

        def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted
        ) -> None:
            if event.option_list.id != "stages":
                return
            oid = event.option.id or ""
            if oid == "all" or oid.startswith("stage:"):
                self.stage = oid.partition(":")[2]
                self.show_stage()

        def on_option_list_option_selected(
            self, event: OptionList.OptionSelected
        ) -> None:
            if event.option_list.id != "stages":
                return
            oid = event.option.id or ""
            if oid == "new":
                self.action_new()
            elif oid == "settings":
                self.action_settings()
            elif oid == "commands":
                self.action_commands()
            else:
                self.query_one("#pkgs").focus()

        def stage_packages(self) -> list[packages.LocalPackage]:
            if self.stage:
                pkgs = self.groups.get(self.stage, [])
            else:
                pkgs = sorted(
                    (p for v in self.groups.values() for p in v), key=lambda p: p.crate
                )
            text = self.query_one("#filter", Input).value.strip().lower()
            return [p for p in pkgs if text in p.crate.lower()] if text else pkgs

        def show_stage(self) -> None:
            total = sum(len(v) for v in self.groups.values())
            title = self.query_one("#stage-title", Static)
            blurb = self.query_one("#stage-blurb", Static)
            if not total:
                title.update(Text("Nothing packaged here yet", style="bold"))
                blurb.update(
                    Text(
                        "Press n (or pick '+ Package something new') and name the crates,"
                        " or the Rust project, you want in Fedora."
                    )
                )
            elif self.stage:
                st = tui_overview.STAGES[self.stage]
                title.update(
                    Text(f"{st.label}  ({len(self.groups[self.stage])})", style="bold")
                )
                blurb.update(Text(st.blurb))
            else:
                title.update(Text(f"All packages  ({total})", style="bold"))
                blurb.update(
                    Text("Every package and the stage it is at: ⏎ shows one in detail.")
                )
            lst = self.query_one("#pkgs", PackageList)
            picked = set(lst.selected)
            was = lst.highlighted
            lst.clear_options()
            for p in self.stage_packages():
                prompt = Text(f"{p.crate:<32}", style="bold")
                prompt.append(f"{p.version or '-':<14}")
                if not self.stage:
                    prompt.append(tui_overview.tui_stage(p).label, style="cyan")
                elif p.limited:
                    prompt.append(p.scope.split(" (")[0], style="dim")
                lst.add_option(Selection(prompt, p.crate, p.crate in picked))
            if lst.option_count:
                # a refilled list has no highlight, and space/d/enter act on it
                lst.highlighted = min(was or 0, lst.option_count - 1)
            self.update_hint()

        def on_selection_list_selection_toggled(self, event) -> None:
            self.update_hint()

        def on_selection_list_selected_changed(self, event) -> None:
            self.update_hint()

        def on_selection_list_selection_highlighted(self, event) -> None:
            self.update_hint()

        def chosen(self) -> list[str]:
            """The packages an action applies to: the selection, else the highlighted one."""
            lst = self.query_one("#pkgs", PackageList)
            if lst.selected:
                return sorted(lst.selected)
            if lst.highlighted is not None and lst.option_count:
                return [lst.get_option_at_index(lst.highlighted).value]
            return []

        def stage_of(self, crates: list[str]) -> str:
            if self.stage:
                return self.stage
            pkgs = packages.local_packages(self.app.root)
            stages = {tui_overview.tui_stage(pkgs[c]).name for c in crates if c in pkgs}
            return stages.pop() if len(stages) == 1 else ""

        def update_hint(self) -> None:
            hint = Text()
            crates = self.chosen()
            lst = self.query_one("#pkgs", PackageList)
            if crates:
                who = f"{len(crates)} selected" if lst.selected else crates[0]
                if self.stage:
                    verb = tui_actions.ACTIONS[
                        tui_overview.STAGES[self.stage].action
                    ].verb
                    hint.append(f"⏎ {verb} for {who}", style="bold")
                else:
                    hint.append(f"⏎ details of {crates[0]}", style="bold")
                hint.append("    space picks more packages", style="dim")
            self.query_one("#hint", Static).update(hint)

        def action_primary(self) -> None:
            crates = self.chosen()
            if not crates:
                return
            if not self.stage:
                self.app.push_screen(PackageScreen(crates[0]))
                return
            self.app.open_action(tui_overview.STAGES[self.stage].action, crates)

        def action_details(self) -> None:
            lst = self.query_one("#pkgs", PackageList)
            if lst.highlighted is not None and lst.option_count:
                self.app.push_screen(
                    PackageScreen(lst.get_option_at_index(lst.highlighted).value)
                )

        def action_other(self) -> None:
            crates = self.chosen()
            stage = self.stage_of(crates)
            keys = tui_actions.STAGE_ACTIONS.get(stage) or [
                k
                for k in tui_actions.ACTIONS
                if k not in ("resolve", "workspace", "init", "doctor")
            ]

            def chosen(key: str | None) -> None:
                if key:
                    self.app.open_action(key, crates)

            self.app.push_screen(ActionMenu(keys, crates), chosen)

        def action_toggle_all(self) -> None:
            lst = self.query_one("#pkgs", PackageList)
            if len(lst.selected) == lst.option_count:
                lst.deselect_all()
            else:
                lst.select_all()

        def action_filter(self) -> None:
            f = self.query_one("#filter", Input)
            f.display = True
            f.focus()

        def on_input_changed(self, event: Input.Changed) -> None:
            if event.input.id == "filter":
                self.show_stage()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            if event.input.id == "filter":
                if not event.value:
                    event.input.display = False
                self.query_one("#pkgs").focus()

        def action_new(self) -> None:
            self.app.push_screen(NewScreen())

        def action_settings(self) -> None:
            self.app.push_screen(SettingsScreen())

        def action_commands(self) -> None:
            crates = (
                self.chosen() if self.query_one("#pkgs", PackageList).selected else []
            )

            def chosen(name: str | None) -> None:
                if name:
                    self.app.push_screen(
                        ActionSheet(raw_action(name), crates, expand=True)
                    )

            self.app.push_screen(CommandMenu(), chosen)

        def action_refresh(self) -> None:
            self.refresh_board()

    def raw_action(name: str) -> tui_actions.TuiAction:
        """A command as the CLI has it, for "All commands": every option, its own
        help, nothing preset; named by its action when it has one."""
        verb = tui_actions.action_for(name).verb
        return tui_actions.TuiAction(name, verb, name, commands[name][0])

    class ActionMenu(ModalScreen):
        """The other actions that fit the picked packages' stage."""

        BINDINGS = [Binding("escape", "dismiss_none", "Back")]

        def __init__(self, keys: list[str], crates: list[str]) -> None:
            super().__init__()
            self.keys, self.crates = keys, crates

        def compose(self) -> ComposeResult:
            with Vertical(classes="menu"):
                yield Label(
                    f"Actions for {crates_text(self.crates) or 'the root'}",
                    classes="menu-title",
                )
                opts = []
                for k in self.keys:
                    a = tui_actions.ACTIONS[k]
                    p = Text(a.verb, style="bold")
                    p.append("\n  " + a.explain, style="dim")
                    opts.append(Option(p, id=k))
                yield OptionList(*opts)

        def on_option_list_option_selected(
            self, event: OptionList.OptionSelected
        ) -> None:
            self.dismiss(event.option.id)

        def action_dismiss_none(self) -> None:
            self.dismiss(None)

    class CommandMenu(ModalScreen):
        """Every rust-deps command, for those who know which one they want."""

        BINDINGS = [Binding("escape", "dismiss_none", "Back")]

        def compose(self) -> ComposeResult:
            with Vertical(classes="menu"):
                yield Label(
                    "All commands (every option, as on the command line)",
                    classes="menu-title",
                )
                opts = []
                for name, (brief, _, _) in commands.items():
                    p = Text(f"{name:<16}", style="bold")
                    p.append(brief, style="dim")
                    opts.append(Option(p, id=name))
                yield OptionList(*opts)

        def on_option_list_option_selected(
            self, event: OptionList.OptionSelected
        ) -> None:
            self.dismiss(event.option.id)

        def action_dismiss_none(self) -> None:
            self.dismiss(None)

    # ---------------------------------------------------------------- details

    class PackageScreen(Screen):
        """One package: what it has, its next step, everything else it can do."""

        BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

        def __init__(self, crate: str) -> None:
            super().__init__()
            self.crate = crate
            self.items: list[tuple[str, str]] = []

        def compose(self) -> ComposeResult:
            with Vertical(id="details"):
                yield Static("", id="pkg-title")
                yield Static("", id="pkg-checklist")
                yield Label("What to do", classes="section")
                yield OptionList(id="pkg-actions")
            yield Footer()

        def on_mount(self) -> None:
            self.fill()
            self.query_one("#pkg-actions").focus()

        def on_screen_resume(self) -> None:
            self.fill()

        def fill(self) -> None:
            pkg = packages.local_packages(self.app.root).get(self.crate)
            if pkg is None:
                self.query_one("#pkg-title", Static).update(
                    f"{self.crate}: no longer under the root"
                )
                return
            stage = tui_overview.tui_stage(pkg)
            title = Text(f"{pkg.crate} {pkg.version or ''}", style="bold")
            title.append(f"  —  {pkg.scope}   ", style="dim")
            title.append(stage.label, style="cyan")
            self.query_one("#pkg-title", Static).update(title)
            check = Text()
            styles = {"✓": "green", "✗": "bold yellow", "·": "dim", "–": "dim"}
            for mark, text in tui_overview.package_checklist(pkg):
                check.append(f" {mark} ", style=styles[mark])
                check.append(text + "\n", style="dim" if mark == "·" else "")
            self.query_one("#pkg-checklist", Static).update(check)
            opts = self.query_one("#pkg-actions", OptionList)
            opts.clear_options()
            self.items = []
            for i, key in enumerate(tui_actions.STAGE_ACTIONS.get(stage.name, [])):
                a = tui_actions.ACTIONS[key]
                p = Text()
                if i == 0:
                    p.append("Next: ", style="bold cyan")
                p.append(a.verb, style="bold")
                p.append("\n" + a.explain, style="dim")
                opts.add_option(Option(p))
                self.items.append(("action", key))
            files = [
                ("rust2rpm.toml", pkg.config_file),
                ("cargo-toml-edits.toml", pkg.edits_file),
            ]
            for label, path in files:
                if path.exists():
                    opts.add_option(Option(Text(f"Edit {label}", style="italic")))
                    self.items.append(("edit", str(path)))
            for name in (f"{pkg.crate}.log", f"{pkg.crate}.recheck.log"):
                log = config.CACHE_DIR / "trial" / name
                if log.exists():
                    opts.add_option(
                        Option(Text(f"View test log {name}", style="italic"))
                    )
                    self.items.append(("log", str(log)))

        def on_option_list_option_selected(
            self, event: OptionList.OptionSelected
        ) -> None:
            kind, what = self.items[event.option_index]
            if kind == "action":
                self.app.open_action(what, [self.crate])
            elif kind == "edit":
                editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
                with self.app.suspend():
                    subprocess.run([*shlex.split(editor), what])
                self.fill()
            else:
                self.app.push_screen(LogScreen(Path(what)))

    class LogScreen(Screen):
        BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

        def compose(self) -> ComposeResult:
            yield Static(Text(str(self.path), style="bold"), classes="section")
            yield RichLog(id="file-log", markup=False, wrap=True)
            yield Footer()

        def on_mount(self) -> None:
            log = self.query_one("#file-log", RichLog)
            for line in self.path.read_text(errors="replace").splitlines()[-3000:]:
                log.write(Text(line, style=tui_line_style(line)))

    # ----------------------------------------------------------- action sheet

    class ActionSheet(Screen):
        """One action ready to run: the inputs that matter, the rest folded away."""

        BINDINGS = [
            Binding("ctrl+r", "run", "Run"),
            Binding("escape", "app.pop_screen", "Back"),
        ]

        def __init__(
            self,
            action: tui_actions.TuiAction,
            crates: list[str],
            values: dict | None = None,
            flags: list[str] | None = None,
            expand: bool = False,
        ) -> None:
            super().__init__()
            self.action = action
            _, self.fields, self.controls = commands[action.cmd]
            self.initial = tui_actions.initial_values(
                action, self.fields, crates, self.app.settings, flags or [], values
            )
            self.expand = expand
            self.widgets: dict[str, object] = {}
            self.selects: dict[int, object] = {}
            self.choice_inputs: dict[int, object] = {}
            self.multi: dict[str, list] = {}
            self.multi_next: dict[str, int] = {}

        def compose(self) -> ComposeResult:
            a = self.action
            yield Static(Text(a.verb, style="bold"), id="sheet-title")
            yield Static(Text(a.explain), id="sheet-explain")
            yield Static("", id="sheet-warning")
            primary = tui_actions.primary_keys(a, self.fields)
            with VerticalScroll(id="sheet-form"):
                for c in self.controls:
                    if any(f.key in primary for f in c.fields):
                        yield from self.render_control(c, primary=True)
                rest = [
                    c
                    for c in self.controls
                    if not any(f.key in primary for f in c.fields)
                ]
                if rest:
                    with Collapsible(title="More options", collapsed=not self.expand):
                        for c in rest:
                            yield from self.render_control(c, primary=False)
            yield Static("", id="preview")
            with Horizontal(id="sheet-bar"):
                yield Button("Run", id="run", variant="primary")
                yield Button("Back", id="back")
            yield Footer()

        def on_mount(self) -> None:
            missing = tui_actions.missing_tools(self.action.cmd, self.app.tools_ok)
            warn = self.query_one("#sheet-warning", Static)
            if missing:
                warn.update(
                    Text(
                        "needs "
                        + ", ".join(t for t, _ in missing)
                        + ": dnf install "
                        + " ".join(p for _, p in missing),
                        style="bold yellow",
                    )
                )
            else:
                warn.display = False
            for gi, select in self.selects.items():
                self.sync_choice(gi)
            self.update_preview()
            first = next(
                iter(self.query("#sheet-form Input, #sheet-form Checkbox")), None
            )
            (first or self.query_one("#run")).focus()

        def label(self, f: tui_form.TuiField) -> str:
            return tui_actions.field_label(self.action, f)

        def render_control(self, c: tui_form.TuiControl, primary: bool):
            if c.kind == "crates":
                for f in c.fields:
                    yield from self.render_field(f, primary)
            elif c.kind == "choice":
                yield from self.render_choice(c)
            elif c.kind == "multi":
                yield from self.render_multi(c.fields[0])
            else:
                yield from self.render_field(c.fields[0], primary)

        def render_field(self, f: tui_form.TuiField, primary: bool):
            v = self.initial.get(f.key)
            if f.kind == "flag":
                label = Text(self.label(f))
                if f.help and (not primary or self.label(f) == f.label):
                    label.append(
                        "  " + textwrap.shorten(f.help, width=60, placeholder=" …"),
                        style="dim",
                    )
                w = Checkbox(label, value=bool(v), compact=True, tooltip=f.help or None)
            else:
                yield Label(
                    Text(self.label(f) + (" *" if f.required else "")),
                    classes="field-label",
                )
                hint = f.help or f.metavar
                if f.positional and f.dest == "crates":
                    hint = "package or crate names, space-separated"
                w = Input(
                    value=" ".join(v) if isinstance(v, list) else str(v or ""),
                    placeholder=textwrap.shorten(hint, width=90, placeholder=" …"),
                    type="number" if f.kind == "number" else "text",
                )
            self.widgets[f.key] = w
            yield w

        def render_choice(self, c: tui_form.TuiControl):
            gi = c.fields[0].group
            yield Label(
                Text("One of: " + " / ".join(f.label for f in c.fields)),
                classes="field-label",
            )
            chosen = next(
                (
                    f.key
                    for f in c.fields
                    if (f.kind == "flag" and self.initial.get(f.key))
                    or (f.kind != "flag" and self.initial.get(f.key))
                ),
                None,
            )
            # the blank sentinel is Select.BLANK in Textual 4 and Select.NULL in 8:
            # only a real choice is passed, so neither has to be named
            select = Select(
                [
                    (
                        Text(
                            f"{f.label}  {textwrap.shorten(f.help, 50, placeholder=' …')}"
                        ),
                        f.key,
                    )
                    for f in c.fields
                ],
                allow_blank=True,
                prompt="(leave as is)",
                name=f"choice:{gi}",
                **({"value": chosen} if chosen else {}),
            )
            value_input = Input(
                value=str(self.initial.get(chosen, "") or "") if chosen else "",
                name=f"choicev:{gi}",
            )
            value_input.display = False
            self.selects[gi] = select
            self.choice_inputs[gi] = value_input
            yield select
            yield value_input

        def sync_choice(self, gi: int) -> None:
            """Show the value input only for a chosen mode that takes a value."""
            key = self.selects[gi].value
            f = next((fl for fl in self.fields if fl.key == key), None)
            w = self.choice_inputs[gi]
            w.display = f is not None and f.kind != "flag"
            if f is not None:
                w.placeholder = f.metavar

        def render_multi(self, f: tui_form.TuiField):
            again = f.options[0] if f.options else f.metavar
            add = Button(
                "+",
                name=f"add:{f.key}",
                classes="multi-add",
                tooltip=f"add another {again}",
            )
            add.can_focus = False
            yield Horizontal(
                Label(
                    Text(self.label(f) + (" *" if f.required else "")),
                    classes="field-label",
                ),
                add,
                classes="multi-head",
            )
            self.multi[f.key] = []
            self.multi_next[f.key] = 0
            v = self.initial.get(f.key) or []
            values = list(v) if isinstance(v, list) else str(v).split()
            rows = [self.make_row(f, value) for value in values or [""]]
            holder = Vertical(*rows, classes="multi-rows", name=f"rows:{f.key}")
            holder.styles.height = len(self.multi[f.key]) * FORM_ROW_HEIGHT
            yield holder

        def make_row(self, f: tui_form.TuiField, value: str):
            n = self.multi_next[f.key]
            self.multi_next[f.key] += 1
            w = Input(
                value=value,
                placeholder=textwrap.shorten(f.help or f.metavar, 90, placeholder=" …"),
            )
            self.multi[f.key].append(w)
            return Horizontal(
                w,
                Button("–", name=f"rm:{f.key}:{n}", classes="multi-remove"),
                classes="multi-row",
            )

        def values(self) -> dict:
            out: dict = {}
            for f in self.fields:
                if f.group >= 0:
                    chosen = self.selects[f.group].value
                    if f.key != chosen:
                        out[f.key] = False if f.kind == "flag" else ""
                    else:
                        out[f.key] = (
                            True
                            if f.kind == "flag"
                            else self.choice_inputs[f.group].value
                        )
                elif f.kind == "multi" and f.key in self.multi:
                    out[f.key] = [
                        r.value.strip() for r in self.multi[f.key] if r.value.strip()
                    ]
                elif (w := self.widgets.get(f.key)) is not None:
                    out[f.key] = w.value
            return out

        def update_preview(self) -> None:
            values = self.values()
            argv = ["rust-deps", self.action.cmd] + tui_form.tui_argv(
                self.fields, values
            )
            missing = [
                self.label(f)
                for f in self.fields
                if f.label in tui_form.tui_missing_required(self.fields, values)
            ]
            text = Text("$ " + " ".join(shlex.quote(a) for a in argv), style="dim")
            if missing:
                text.append(
                    "\nstill needed: " + ", ".join(missing), style="bold yellow"
                )
            self.query_one("#preview", Static).update(text)
            blocked = bool(missing) or bool(
                tui_actions.missing_tools(self.action.cmd, self.app.tools_ok)
            )
            self.query_one("#run", Button).disabled = blocked

        def on_input_changed(self, event: Input.Changed) -> None:
            self.update_preview()

        def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
            self.update_preview()

        def on_select_changed(self, event: Select.Changed) -> None:
            self.sync_choice(int(event.select.name.partition(":")[2]))
            self.update_preview()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self.action_run()

        async def on_button_pressed(self, event: Button.Pressed) -> None:
            name = event.button.name or ""
            if event.button.id == "run":
                self.action_run()
            elif event.button.id == "back":
                self.app.pop_screen()
            elif name.startswith("add:"):
                key = name.partition(":")[2]
                f = next(m for m in self.fields if m.key == key)
                holder = next(
                    w for w in self.query(".multi-rows") if w.name == f"rows:{key}"
                )
                await holder.mount(self.make_row(f, ""))
                holder.styles.height = len(self.multi[key]) * FORM_ROW_HEIGHT
                self.multi[key][-1].focus()
                self.update_preview()
            elif name.startswith("rm:"):
                key = name.split(":")[1]
                rows = self.multi[key]
                row = event.button.parent
                if len(rows) > 1:
                    w = next(r for r in rows if r.parent is row)
                    rows.remove(w)
                    await row.remove()
                    next(
                        w for w in self.query(".multi-rows") if w.name == f"rows:{key}"
                    ).styles.height = (len(rows) * FORM_ROW_HEIGHT)
                else:
                    rows[0].value = ""
                self.update_preview()

        def action_run(self) -> None:
            if self.query_one("#run", Button).disabled:
                return
            values = self.values()
            if tui_actions.remember(self.app.settings, values):
                tui_actions.save_settings(self.app.root, self.app.settings)
                self.notify("Saved to Settings for the next actions")
            crates = str(values.get("crates") or values.get("crate") or "").split()
            argv = tui_form.tui_argv(self.fields, values)
            self.app.switch_screen(RunScreen(self.action, argv, crates))

    # -------------------------------------------------------------------- run

    class RunScreen(Screen):
        """A running command: its live log, then what it means and what is next."""

        BINDINGS = [
            Binding("escape", "back", "Cancel / back"),
            Binding("l", "toggle_log", "Log / summary"),
        ]

        def __init__(
            self, action: tui_actions.TuiAction, argv: list[str], crates: list[str]
        ) -> None:
            super().__init__()
            self.action, self.argv, self.crates = action, argv, crates
            self.proc = None
            self.stdout: list[str] = []
            self.lines: list[str] = []
            self.running = True
            self.steps: list[tuple] = []

        def compose(self) -> ComposeResult:
            title = Text(self.action.verb, style="bold")
            if self.crates:
                title.append("  " + crates_text(self.crates, 6))
            yield Static(title, id="run-title")
            yield Static(
                Text(
                    "$ rust-deps "
                    + " ".join(shlex.quote(a) for a in [self.action.cmd, *self.argv]),
                    style="dim",
                )
            )
            with ContentSwitcher(id="run-view", initial="run-log"):
                yield RichLog(id="run-log", markup=False, wrap=True)
                with Vertical(id="run-summary"):
                    yield Static("", id="verdict")
                    yield DataTable(
                        id="result-table", zebra_stripes=True, cursor_type="row"
                    )
                    yield Label("Next", classes="section", id="next-label")
                    yield OptionList(id="next")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#result-table").display = False
            self.run_command(
                [
                    sys.executable,
                    str(config.SELF),
                    "--root",
                    str(self.app.root),
                    self.action.cmd,
                    *self.argv,
                ]
            )

        def on_unmount(self) -> None:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()

        # A thread worker with subprocess.Popen: Textual's terminal driver reaps
        # child processes itself, which would leave an asyncio subprocess
        # proc.wait() hanging on a zombie.
        @work(thread=True, exclusive=True, group="run")
        def run_command(self, argv: list[str]) -> None:
            code = 1
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",
                    env={**os.environ, "RUST_DEPS_NO_HYPERLINKS": "1"},
                )
                self.proc = proc

                def drain(pipe, is_stderr: bool) -> None:
                    for raw in pipe:
                        line = raw.rstrip("\r\n")
                        self.app.call_from_thread(self.write_line, line, is_stderr)
                    pipe.close()

                err = threading.Thread(
                    target=drain, args=(proc.stderr, True), daemon=True
                )
                err.start()
                drain(proc.stdout, False)
                err.join()
                code = proc.wait()
            except Exception as exc:
                self.app.call_from_thread(self.write_line, f"ERROR: {exc}", True)
            self.app.call_from_thread(self.finish, code)

        def write_line(self, line: str, is_stderr: bool) -> None:
            if not is_stderr:
                self.stdout.append(line)
            self.lines.append(line)
            self.query_one("#run-log", RichLog).write(
                Text(line, style=tui_line_style(line))
            )

        def finish(self, code: int) -> None:
            self.running = False
            self.proc = None
            cancelled = code < 0
            log = self.query_one("#run-log", RichLog)
            log.write(Text(f"exit {code}", style="bold red" if code else "dim"))
            if self.action.cmd == "doctor":
                self.app.tools_ok = (
                    tui_results.parse_tool_states(self.stdout) or self.app.tools_ok
                )
            pb = tui_playbooks.TUI_PLAYBOOKS.get(self.action.cmd)
            parsed, actions = None, []
            if pb and pb.result in tui_playbooks.TUI_RESULT_RENDERERS:
                parsed, actions = tui_playbooks.TUI_RESULT_RENDERERS[pb.result](
                    self.stdout, self.app.root
                )
            proposed = tui_overview.tui_suggested_actions(
                self.lines, self.app.root, commands
            )
            self.steps = tui_actions.merge_steps(
                actions,
                proposed,
                tui_actions.follow_ups(
                    self.app.root, self.crates, after=self.action.key
                ),
            )
            verdict = Text()
            if cancelled:
                verdict.append("■ cancelled", style="bold yellow")
            elif code:
                verdict.append(
                    f"✗ {self.action.verb} failed (exit {code})", style="bold red"
                )
                errors = [ln for ln in self.lines if ln.startswith("ERROR:")]
                for ln in errors[-3:]:
                    verdict.append("\n  " + ln, style="red")
            else:
                verdict.append(f"✓ {self.action.verb} finished", style="bold green")
            problems = tui_actions.action_needed(self.lines)
            if problems:
                verdict.append(
                    f"\n\n{len(problems)} thing(s) to fix by hand (ACTION NEEDED):",
                    style="bold magenta",
                )
                for p in problems[:8]:
                    verdict.append("\n  • " + p)
                if len(problems) > 8:
                    verdict.append(
                        f"\n  … {len(problems) - 8} more in the log (l)", style="dim"
                    )
            verdict.append("\n\nl shows the full log", style="dim")
            self.query_one("#verdict", Static).update(verdict)
            table = self.query_one("#result-table", DataTable)
            table.clear(columns=True)
            table.display = parsed is not None
            if parsed is not None:
                headers, rows = parsed
                table.add_columns(*headers)
                for row in rows:
                    table.add_row(*row)
            nxt = self.query_one("#next", OptionList)
            nxt.clear_options()
            for step in self.steps:
                nxt.add_option(Option(step_prompt(step)))
            nxt.add_option(Option(Text("Back to the board", style="italic")))
            self.query_one("#run-view", ContentSwitcher).current = "run-summary"
            nxt.focus()

        def on_option_list_option_selected(
            self, event: OptionList.OptionSelected
        ) -> None:
            if event.option_index >= len(self.steps):
                self.app.back_to_board()
                return
            cmd, crates, flags, values, _ = self.steps[event.option_index]
            self.app.open_step(cmd, crates, flags, values, replace=True)

        def action_back(self) -> None:
            if self.running:
                if self.proc is not None and self.proc.poll() is None:
                    self.proc.terminate()
                return
            self.app.pop_screen()

        def action_toggle_log(self) -> None:
            if self.running:
                return
            view = self.query_one("#run-view", ContentSwitcher)
            view.current = "run-log" if view.current == "run-summary" else "run-summary"

    # --------------------------------------------------- explain test skips

    class ExplainScreen(Screen):
        """The one manual step of the lifecycle: the reasons of a [tests] table
        'trial --apply' wrote as TODOs, edited in place."""

        BINDINGS = [
            Binding("ctrl+s", "save", "Save"),
            Binding("escape", "app.pop_screen", "Back"),
        ]

        def __init__(self, crates: list[str]) -> None:
            super().__init__()
            self.crates = crates
            self.inputs: dict[str, list] = {}

        def compose(self) -> ComposeResult:
            yield Static(Text("Explain test skips", style="bold"), id="sheet-title")
            yield Static(
                Text(
                    "Each skipped test target needs a reason in rust2rpm.toml; it ends up in"
                    " the spec's comments.  Only the comments of [tests] change."
                ),
                id="sheet-explain",
            )
            with VerticalScroll(id="sheet-form"):
                pkgs = packages.local_packages(self.app.root)
                for crate in sorted(
                    self.crates or tui_overview.tests_todo_crates(self.app.root)
                ):
                    pkg = pkgs.get(crate)
                    if pkg is None or not pkg.config_file.exists():
                        continue
                    text = pkg.config_file.read_text()
                    sm = tui_overview._TESTS_SECTION_RE.search(text)
                    comments = tui_overview.tests_table_comments(text)
                    if not sm or not comments:
                        continue
                    context = [ln for ln in sm.group(0).splitlines()]
                    context = context[
                        : next(
                            (
                                i
                                for i, ln in enumerate(context)
                                if ln.startswith("comments")
                            ),
                            len(context),
                        )
                    ]
                    yield Static(Text(crate, style="bold cyan"))
                    yield Static(Text("\n".join(context), style="dim"))
                    rows = []
                    for i, c in enumerate(comments):
                        m = re.search(r"explain why (?:these )?(\S+)", c)
                        yield Label(
                            m.group(1) if m else f"reason {i + 1}",
                            classes="field-label",
                        )
                        w = Input(value=c)
                        rows.append(w)
                        yield w
                    self.inputs[crate] = rows
            with Horizontal(id="sheet-bar"):
                yield Button("Save and regenerate", id="save", variant="primary")
                yield Button("Back", id="back")
            yield Footer()

        def on_mount(self) -> None:
            first = next(iter(self.query(Input)), None)
            if first is not None:
                first.focus()
            else:
                self.query_one("#sheet-explain", Static).update(
                    Text("No TODO reasons left in these packages' [tests] tables.")
                )
                self.query_one("#save", Button).disabled = True

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "save":
                self.action_save()
            else:
                self.app.pop_screen()

        def action_save(self) -> None:
            pkgs = packages.local_packages(self.app.root)
            left = []
            for crate, rows in self.inputs.items():
                pkg = pkgs[crate]
                reasons = [w.value for w in rows]
                pkg.config_file.write_text(
                    tui_overview.replace_tests_comments(
                        pkg.config_file.read_text(), reasons
                    )
                )
                if any("TODO" in r for r in reasons):
                    left.append(crate)
            if left:
                self.notify(
                    f"saved; still TODO in {', '.join(left)}", severity="warning"
                )
            done = [c for c in self.inputs if c not in left]
            if done:
                # the reasons go into the spec's comments: regenerate it right away
                self.app.switch_screen(
                    RunScreen(tui_actions.ACTIONS["regen"], done, done)
                )

    # -------------------------------------------------------------- settings

    class SettingsScreen(Screen):
        BINDINGS = [
            Binding("ctrl+s", "save", "Save"),
            Binding("escape", "app.pop_screen", "Back"),
        ]

        FIELDS = [
            ("project", "COPR project", "user/project — where builds are published"),
            (
                "chroots",
                "Chroots",
                "space-separated, e.g. fedora-rawhide-x86_64 fedora-45-x86_64",
            ),
            ("fas", "FAS account", "your Fedora account, for review requests"),
            ("dist_git_dir", "Directory for dist-git checkouts", "e.g. ~/src/fedora"),
        ]

        def compose(self) -> ComposeResult:
            yield Static(Text("Settings", style="bold"), id="sheet-title")
            yield Static(
                Text(
                    f"Asked once for {self.app.root}; every action starts from them."
                    f"  Kept in {config.CACHE_DIR / tui_actions.SETTINGS_FILE}."
                ),
                id="sheet-explain",
            )
            s = self.app.settings
            with VerticalScroll(id="sheet-form"):
                for key, label, hint in self.FIELDS:
                    v = getattr(s, key)
                    yield Label(label, classes="field-label")
                    yield Input(
                        value=" ".join(v) if isinstance(v, list) else v,
                        placeholder=hint,
                        id=f"set-{key}",
                    )
            with Horizontal(id="sheet-bar"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Back", id="back")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#set-project", Input).focus()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "save":
                self.action_save()
            else:
                self.app.pop_screen()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self.action_save()

        def action_save(self) -> None:
            s = self.app.settings
            for key, _, _ in self.FIELDS:
                v = self.query_one(f"#set-{key}", Input).value.strip()
                setattr(s, key, v.split() if key == "chroots" else v)
            tui_actions.save_settings(self.app.root, s)
            self.notify("Settings saved")
            self.app.pop_screen()

    # ------------------------------------------------------------ onboarding

    class NewScreen(Screen):
        """Start from what the packager wants in Fedora: crates, or a project."""

        BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

        def compose(self) -> ComposeResult:
            yield Static(Text("Package something new", style="bold"), id="sheet-title")
            yield Static(
                Text(
                    "Name the crates you need (serde_json_path, jsonschema@^0.30), or give the"
                    " directory or Cargo.toml of the Rust project you want to build in Fedora."
                    "  The first step finds what Fedora is missing; then you create the packages."
                ),
                id="sheet-explain",
            )
            with VerticalScroll(id="sheet-form"):
                yield Label("Crates, or a project path", classes="field-label")
                yield Input(id="what", placeholder="crate names, or ~/src/myproject")
                yield Static("", id="new-plan")
            with Horizontal(id="sheet-bar"):
                yield Button("Find what is missing", id="find", variant="primary")
                yield Button("More options…", id="more")
                yield Button("Create packages directly", id="init")
                yield Button("Back", id="back")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#what", Input).focus()

        def plan(self) -> tuple[str, list[str], dict]:
            text = self.query_one("#what", Input).value.strip()
            if tui_actions.is_project_path(text):
                return "workspace", [], {"projects": tui_form.expand_user_path(text)}
            return "resolve", text.split(), {}

        def on_input_changed(self, event: Input.Changed) -> None:
            key, crates, values = self.plan()
            a = tui_actions.ACTIONS[key]
            what = "a project on disk" if key == "workspace" else "crate names"
            self.query_one("#new-plan", Static).update(
                Text(f"{what}: '{a.verb}' — {a.explain}", style="dim")
                if event.value.strip()
                else ""
            )

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self.start("find")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "back":
                self.app.pop_screen()
            else:
                self.start(event.button.id)

        def start(self, how: str) -> None:
            key, crates, values = self.plan()
            if not crates and not values:
                self.notify("name some crates or a project first", severity="warning")
                return
            if how == "init":
                key, values = "init", {}
            action = tui_actions.ACTIONS[key]
            if how == "find":
                fields = commands[action.cmd][1]
                init = tui_actions.initial_values(
                    action, fields, crates, self.app.settings, values=values
                )
                self.app.switch_screen(
                    RunScreen(action, tui_form.tui_argv(fields, init), crates)
                )
            else:
                self.app.switch_screen(ActionSheet(action, crates, values=values))

    # ------------------------------------------------------------------- app

    class RustDepsTUI(App):
        TITLE = "rust-deps"
        CSS = """
            Screen { background: $surface; }
            #topbar { height: 1; padding: 0 1; background: $panel; }
            #board { height: 1fr; }
            #stages { width: 34; height: 1fr; }
            #stage-pane { width: 1fr; padding: 0 1; }
            #stage-title { height: 1; margin-top: 1; }
            #stage-blurb { color: $text-muted; height: auto; margin-bottom: 1; }
            #filter { height: 3; }
            #pkgs { height: 1fr; }
            #hint { height: 1; }
            .section { color: $text-muted; margin-top: 1; }
            .field-label { color: $text-muted; height: 1; margin-top: 1; }
            #details { padding: 0 1; }
            #pkg-title { margin-top: 1; height: auto; }
            #pkg-checklist { margin-top: 1; height: auto; }
            #pkg-actions { height: 1fr; }
            #sheet-title { padding: 1 1 0 1; height: auto; }
            #sheet-explain { padding: 0 1; color: $text-muted; height: auto; }
            #sheet-warning { padding: 0 1; height: auto; }
            #sheet-form { height: 1fr; padding: 0 1; }
            #sheet-form Input { width: 100%; height: 3; }
            #sheet-form Select { width: 100%; }
            #sheet-form Checkbox { height: 1; margin-top: 1; }
            #sheet-form Collapsible { margin-top: 1; }
            #preview { padding: 0 1; height: auto; }
            #sheet-bar { height: auto; padding: 0 1; }
            #sheet-bar Button { margin-right: 1; }
            .multi-head { width: 100%; height: 2; }
            .multi-head Label { width: 1fr; }
            .multi-add { width: 3; min-width: 3; height: 1; padding: 0; border: none; margin-top: 1; }
            .multi-rows { width: 100%; }
            .multi-row { width: 100%; height: 3; }
            .multi-row Input { width: 1fr; }
            .multi-remove { width: 3; min-width: 3; }
            #run-title { padding: 1 1 0 1; height: auto; }
            #run-view { height: 1fr; }
            #run-summary { padding: 0 1; }
            #verdict { height: auto; margin-top: 1; }
            #result-table { height: auto; max-height: 16; margin-top: 1; }
            #next { height: 1fr; }
            .menu { width: 90; height: auto; max-height: 80%; border: thick $accent; background: $surface; padding: 0 1; }
            .menu-title { margin-bottom: 1; }
            /* an unchecked box shows no mark: a dim X read as checked */
            ToggleButton > .toggle--button { color: $panel; background: $panel; }
            ToggleButton.-on > .toggle--button { color: $success; }
            SelectionList > .selection-list--button { color: $panel; background: $panel; }
            SelectionList > .selection-list--button-highlighted { color: $panel; background: $panel; }
            SelectionList > .selection-list--button-selected { color: $success; background: $panel; }
            SelectionList > .selection-list--button-selected-highlighted { color: $success; background: $panel; }
            .menu OptionList { height: auto; max-height: 30; }
            ActionMenu, CommandMenu { align: center middle; }
        """

        def __init__(self, root: Path) -> None:
            super().__init__()
            self.root = root
            self.settings = tui_actions.load_settings(root)
            self.tools_ok: dict[str, bool] = {}

        def on_mount(self) -> None:
            self.push_screen(BoardScreen())
            self._probe_tools()

        @work(thread=True, exclusive=True, group="probe")
        def _probe_tools(self) -> None:
            argv = [
                sys.executable,
                str(config.SELF),
                "--root",
                str(self.root),
                "doctor",
            ]
            try:
                res = subprocess.run(
                    argv, capture_output=True, text=True, errors="replace"
                )
                tools = tui_results.parse_tool_states(res.stdout.splitlines())
            except Exception:
                tools = {}
            if tools:
                self.call_from_thread(self._apply_tools, tools)

        def _apply_tools(self, tools: dict[str, bool]) -> None:
            self.tools_ok = tools
            for screen in self.screen_stack:
                if isinstance(screen, BoardScreen):
                    screen.update_topbar()

        def open_action(
            self, key: str, crates: list[str], replace: bool = False
        ) -> None:
            a = tui_actions.ACTIONS[key]
            self.open_step(a.cmd or "tests-edit", crates, list(a.flags), {}, replace)

        def open_step(
            self,
            cmd: str,
            crates: list[str],
            flags: list[str],
            values: dict,
            replace: bool = False,
        ) -> None:
            action = tui_actions.action_for(cmd, flags)
            if action.key == "explain-skips":
                screen = ExplainScreen(crates)
            elif action.cmd not in commands:
                return
            else:
                screen = ActionSheet(action, crates, values=values, flags=flags)
            (self.switch_screen if replace else self.push_screen)(screen)

        def back_to_board(self) -> None:
            while (
                not isinstance(self.screen, BoardScreen) and len(self.screen_stack) > 2
            ):
                self.pop_screen()

    # the screens are built here, with textual imported: tests reach them through the app
    RustDepsTUI.RunScreen = RunScreen
    RustDepsTUI.ActionSheet = ActionSheet
    return RustDepsTUI


def cmd_tui(args) -> None:
    try:
        app = make_tui_app()
    except ImportError:
        util.die("rust-deps tui needs python3-textual: dnf install python3-textual")
    app(args.root).run()
