"""rust-deps TUI application: the Textual app itself."""

from __future__ import annotations

from pathlib import Path
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import textwrap

from . import cli
from . import config
from . import packages
from . import tui_form
from . import tui_overview
from . import tui_playbooks
from . import tui_results
from . import util

# 'rust-deps tui' presents the whole workflow in a text UI (python3-textual).
# Its home screen is an Overview: the state of the packages under the root and
# the next steps implied by the package lifecycle graph (TUI_STAGES), each
# jumping to the command with the crates picked.  Forms are generated from the
# same argparse parser as the command line, so the UI cannot drift from it; the
# parser's structure decides the
# widget (crate picker, mode choice for exclusive groups, rows for repeatable
# options, Run gated on required arguments).  What each command's screen then
# does is data, not per-screen code: TUI_PLAYBOOKS names the result renderer
# that shapes its output into a table and next steps (TUI_RESULT_RENDERERS),
# the tools it needs (the sidebar gates on 'doctor'), and which form values
# carry over from the previous run (the COPR project, the target chroot).
# Next-step jumps carry the crates, flags and literal option values the tool
# itself named.  Each command runs as a 'rust-deps' subprocess whose output
# streams into the view, with block headers and verdict words colored.  The
# Textual APIs used here exist unchanged in Textual 4 (Fedora 44) and Textual 8.

TUI_SECTIONS = [
    ("Setup", ["doctor", "status"]),
    ("Missing crates", ["resolve", "workspace", "order", "check-targets"]),
    ("Create", ["init", "regen", "update"]),
    ("Test", ["trial"]),
    ("Build", ["srpm", "mock-chain", "copr", "tmt"]),
    ("COPR builds", ["copr-status", "copr-log"]),
    ("Review", ["review", "review-request", "review-plan", "review-status"]),
    ("In Fedora", ["adopt", "dist-git"]),
]


_TUI_LINE_STYLES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^ERROR:"), "bold red"),
    (re.compile(r"^ACTION NEEDED:"), "bold magenta"),
    (re.compile(r"^WARNING:"), "yellow"),
    (re.compile(r"^=+ "), "bold cyan"),  # '== package' and '=== stage' blocks
    (re.compile(r"^\$ "), "dim"),  # echoed tool commands
    (re.compile(r"^(NEW|UPDATE|FEATURES)(?=\s)"), "cyan"),  # resolve verdicts
    (re.compile(r"^(SYSTEM|PACKAGED|WORKSPACE)(?=\s)"), "cyan"),  # where a workspace crate comes from
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


# The height, in terminal rows, of one repeatable-argument row (an Input with its
# tall border).  '#form' is capped so a long form scrolls, and Textual shrinks any
# container whose height is 'auto' to whatever space is left: a row or the holder
# of rows would collapse to zero rows while the Buttons beside them keep their
# three.  Every widget a form mounts therefore states a height, and a row holder
# is sized from its row count (see _set_rows_height).  The matching CSS rules are
# in RustDepsTUI.CSS.
FORM_ROW_HEIGHT = 3


def make_tui_app() -> type:
    """The Textual app class.  Imports textual, so only 'tui' needs it."""
    import shlex
    import threading
    from rich.text import Text
    from textual import work
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.widgets import (
        Button,
        Checkbox,
        ContentSwitcher,
        DataTable,
        Footer,
        Header,
        Input,
        Label,
        OptionList,
        RichLog,
        Select,
        Static,
        Tabs,
    )
    from textual.widgets.option_list import Option
    from textual.widgets._tabs import Tab  # same signature in Textual 4 and 8

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

    class RustDepsTUI(App):
        TITLE = "rust-deps"
        CSS = """
            #body { height: 1fr; }
            #sidebar { width: 24; }
            #sidebar-title { padding: 0 1; }
            #commands { height: 1fr; }
            #screens { width: 1fr; }
            #overview { width: 1fr; padding: 0 1; }
            #overview .field-label { color: $text-muted; }
            #pkg-table { height: 1fr; }
            #suggestions { height: 1fr; }
            #work { width: 1fr; }
            #cmd-help { height: auto; max-height: 3; padding: 0 1; }
            #form { height: auto; max-height: 16; }
            #form-crates, #form-options { width: 1fr; padding: 0 1; overflow-y: auto; }
            #form .field-label { color: $text-muted; height: 1; }
            #form Input { width: 100%; height: 3; }
            #form Select { width: 100%; height: 3; }
            #form Checkbox { height: 1; }
            #form .multi-head { width: 100%; height: 1; }
            #form .multi-head Label { width: 1fr; }
            #form .multi-add { width: 3; min-width: 3; height: 1; padding: 0 1; border: none; }
            #form .multi-rows { width: 100%; }
            #form .multi-row { width: 100%; height: 3; }
            #form .multi-row Input { width: 1fr; }
            #form .multi-remove { width: 3; min-width: 3; }
            #runbar { padding: 0 1; }
            #preview { width: 1fr; }
            #view { height: 1fr; }
            #table { width: 1fr; height: 1fr; }
            #result-table { height: 1fr; }
            #result-actions-label { color: $text-muted; }
            #result-actions { height: auto; max-height: 9; }
            #tests-edit { width: 1fr; padding: 0 1; }
            #tests-edit .field-label { color: $text-muted; }
            #tests-edit-table { height: auto; max-height: 12; }
            #tests-edit-rows { height: auto; max-height: 14; overflow-y: auto; }
            #tests-edit-rows Input { width: 100%; height: 3; }
            #tests-actions { height: auto; max-height: 8; }
        """
        BINDINGS = [
            ("ctrl+r", "run_command", "Run"),
            ("escape", "cancel_command", "Cancel"),
        ]

        def __init__(self, root: Path) -> None:
            super().__init__()
            self.root = root
            self._cmd: str | None = None
            self._fields: list[tui_form.TuiField] = []
            self._widgets: dict[str, object] = {}
            self._proc = None
            self._stdout: list[str] = []
            self._rebuild = None
            self._cmd_running = False  # App._running is Textual's own; do not shadow it
            self._crate_boxes: dict[str, object] = {}
            self._crate_extra = None
            self._selects: dict[int, object] = {}
            self._choice_inputs: dict[int, object] = {}
            self._multi_rows: dict[str, list] = {}
            self._multi_next: collections.Counter = collections.Counter()
            self._gen = 0
            self._prefill: set[str] = set()
            self._prefill_flags: set[str] = set()
            self._prefill_values: dict[str, str] = {}
            self._last_form_values: dict[str, str] = {}
            self._tools_ok: dict[str, bool] = {}
            self._suggestions: list[
                tuple[str, list[str], list[str], dict[str, str], str]
            ] = []
            self._result_actions: list[
                tuple[str, list[str], list[str], dict[str, str], str]
            ] = []
            self._tests_actions: list[
                tuple[str, list[str], list[str], dict[str, str], str]
            ] = []
            self._edit_inputs: dict[str, list] = {}

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal(id="body"):
                with Vertical(id="sidebar"):
                    yield Label("Commands", id="sidebar-title")
                    yield OptionList(id="commands")
                with ContentSwitcher(id="screens", initial="overview"):
                    with Vertical(id="overview"):
                        yield Label("packages under the root", classes="field-label")
                        yield DataTable(
                            id="pkg-table", zebra_stripes=True, cursor_type="row"
                        )
                        yield Label(
                            "next steps (select to jump with the crates picked)",
                            classes="field-label",
                        )
                        yield OptionList(id="suggestions")
                    with Vertical(id="work"):
                        yield Static("", id="cmd-help")
                        # crate picker and options sit side by side: both stay visible
                        # instead of the options hiding below a long crate list
                        with Horizontal(id="form"):
                            yield Vertical(id="form-crates")
                            yield Vertical(id="form-options")
                        with Horizontal(id="runbar"):
                            yield Button("Run", id="run", variant="primary")
                            yield Button("Cancel", id="cancel", disabled=True)
                            yield Static("", id="preview")
                        # tab ids must match the ContentSwitcher children's ids
                        yield Tabs(
                            Tab("Log", id="log"), Tab("Table", id="table"), id="tabs"
                        )
                        with ContentSwitcher(id="view", initial="log"):
                            yield RichLog(id="log", markup=False, wrap=True)
                            with Vertical(id="table"):
                                yield DataTable(
                                    id="result-table",
                                    zebra_stripes=True,
                                    cursor_type="row",
                                )
                                yield Label("", id="result-actions-label")
                                yield OptionList(id="result-actions")
                    # the one manual step of the lifecycle, made editable in the UI:
                    # the reasons of a [tests] table 'trial --apply' wrote as TODOs
                    with Vertical(id="tests-edit"):
                        yield Label(
                            "rust2rpm.toml [tests] reasons",
                            id="tests-edit-title",
                            classes="field-label",
                        )
                        yield Static("", id="tests-edit-table")
                        yield Vertical(id="tests-edit-rows")
                        with Horizontal(id="tests-edit-bar"):
                            yield Button(
                                "Save reasons", id="tests-save", variant="primary"
                            )
                            yield Button("Back to overview", id="tests-back")
                        yield Label(
                            "after saving (select to jump)", classes="field-label"
                        )
                        yield OptionList(id="tests-actions")
            yield Footer()

        def on_mount(self) -> None:
            self.sub_title = (
                f"{self.root}  ({len(packages.local_packages(self.root))} packages)"
            )
            self._build_sidebar()
            self.query_one("#run", Button).disabled = True  # until a command is picked
            self._refresh_overview()
            # gate the sidebar as soon as a 'doctor' probe has answered
            self._probe_tools()

        def _build_sidebar(self) -> None:
            options = self.query_one("#commands", OptionList)
            options.clear_options()
            listed = set()
            options.add_option(Option("Overview", id="overview"))
            for title, names in TUI_SECTIONS:
                options.add_option(Option(f"[b]{title}[/b]", disabled=True))
                for name in names:
                    if name in commands:
                        options.add_option(self._command_option(name))
                        listed.add(name)
            for name in sorted(
                set(commands) - listed
            ):  # commands added later still show up
                options.add_option(self._command_option(name))

        def _command_option(self, name: str) -> Option:
            # a command whose playbook tools are missing stays visible but unselectable
            missing = [
                t
                for t in tui_playbooks.TUI_PLAYBOOKS.get(
                    name, tui_playbooks.TuiPlaybook()
                ).requires
                if self._tools_ok.get(t) is False
            ]
            if missing:
                return Option(
                    f"{name}  (needs {', '.join(missing)})", id=name, disabled=True
                )
            return Option(name, id=name)

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
                self.call_from_thread(self._apply_tool_gates, tools)

        def _apply_tool_gates(self, tools: dict[str, bool]) -> None:
            self._tools_ok = tools
            self._build_sidebar()

        def on_option_list_option_selected(
            self, event: OptionList.OptionSelected
        ) -> None:
            if event.option_list.id in (
                "suggestions",
                "result-actions",
                "tests-actions",
            ):
                src = {
                    "suggestions": self._suggestions,
                    "result-actions": self._result_actions,
                    "tests-actions": self._tests_actions,
                }[event.option_list.id]
                cmd, crates, flags, values, _ = src[event.option_index]
                if cmd == "tests-edit":
                    self._open_tests_editor(crates)
                    return
                self.query_one("#screens", ContentSwitcher).current = "work"
                self._show(cmd, prefill=crates, flags=flags, values=values)
            elif event.option_id == "overview":
                self._refresh_overview()
                self.query_one("#screens", ContentSwitcher).current = "overview"
            else:
                self.query_one("#screens", ContentSwitcher).current = "work"
                self._show(event.option_id)

        def _fill_actions(
            self,
            options: OptionList,
            actions: list[tuple[str, list[str], list[str], dict[str, str], str]],
        ) -> None:
            options.clear_options()
            for i, (cmd, crates, flags, values, why) in enumerate(actions):
                fields = commands[cmd][1] if cmd in commands else []
                picked = " ".join(crates[:3]) + (" …" if len(crates) > 3 else "")
                bits = [
                    f.label
                    for key in flags
                    if (f := next((fl for fl in fields if fl.key == key), None))
                ]
                for key, val in values.items():
                    f = next((fl for fl in fields if fl.key == key), None)
                    if f is None:
                        continue
                    if f.positional:  # a single-positional crate reads as the crate
                        picked = (picked + " " + val).strip()
                    else:
                        bits.append(f"{f.label} {val}")
                if bits:
                    picked += ("  " if picked else "") + "  ".join(bits)
                prompt = Text()  # a Text renderable: reasons may contain '[tests]' etc.
                prompt.append(cmd + " ", style="bold")
                if picked:
                    prompt.append(picked + "  ")
                prompt.append(why, style="dim")
                options.add_option(Option(prompt, id=f"act-{i}"))

        def _refresh_overview(self) -> None:
            headers, rows = tui_overview.tui_package_rows(self.root)
            table = self.query_one("#pkg-table", DataTable)
            table.clear()
            table.add_columns(*headers)
            for row in rows:
                table.add_row(*row)
            self._suggestions = tui_overview.tui_suggestions(self.root)
            self._fill_actions(
                self.query_one("#suggestions", OptionList), self._suggestions
            )

        def _open_tests_editor(self, crates: list[str]) -> None:
            self._cancel_proc()
            self._cmd = None
            self._fields, self._widgets = [], {}
            self._crate_extra = None
            self._crate_boxes = {}
            self.query_one("#run", Button).disabled = True
            self.query_one("#screens", ContentSwitcher).current = "tests-edit"
            self._tests_editor = self.run_worker(
                self._build_tests_editor(crates), exclusive=True, group="form"
            )

        async def _build_tests_editor(self, crates: list[str]) -> None:
            """Show each TODO comment of the selected packages as an editable row,
            with the rest of their [tests] tables as read-only context."""
            self._gen += 1
            pkgs = packages.local_packages(self.root)
            rows = self.query_one("#tests-edit-rows", Vertical)
            await rows.remove_children()
            context: list[str] = []
            self._edit_inputs = {}
            for crate in sorted(crates or tui_overview.tests_todo_crates(self.root)):
                pkg = pkgs.get(crate)
                if pkg is None or not pkg.config_file.exists():
                    continue
                text = pkg.config_file.read_text()
                sm = tui_overview._TESTS_SECTION_RE.search(text)
                comments = tui_overview.tests_table_comments(text)
                if not sm or not comments:
                    continue
                context.append(f"== {crate}  {pkg.config_file}")
                for ln in sm.group(0).splitlines():
                    if ln.startswith("comments"):
                        break
                    context.append(ln)
                inputs = []
                for i, c in enumerate(comments):
                    m = re.search(r"explain why (?:these )?(\S+)", c)
                    label = (
                        f"{crate}  {m.group(1)}" if m else f"{crate}  reason {i + 1}"
                    )
                    widget = Input(value=c, id=self._wid(f"te-{crate}-{i}"))
                    await rows.mount(Label(label, classes="field-label"), widget)
                    inputs.append(widget)
                self._edit_inputs[crate] = inputs
            self.query_one("#tests-edit-table", Static).update("\n".join(context))
            chosen = list(self._edit_inputs)
            self._tests_actions = [
                (
                    "regen",
                    chosen,
                    [],
                    {},
                    "reasons written: regenerate the spec so they appear in it",
                ),
                (
                    "srpm",
                    chosen,
                    [],
                    {},
                    "or continue to the SRPM once the spec is regenerated",
                ),
            ]
            self._fill_actions(
                self.query_one("#tests-actions", OptionList), self._tests_actions
            )

        def _save_tests_reasons(self) -> None:
            pkgs = packages.local_packages(self.root)
            written = []
            for crate, inputs in self._edit_inputs.items():
                pkg = pkgs.get(crate)
                if pkg is None:
                    continue
                pkg.config_file.write_text(
                    tui_overview.replace_tests_comments(
                        pkg.config_file.read_text(), [w.value for w in inputs]
                    )
                )
                written.append(crate)
            self._refresh_overview()
            self.query_one("#tests-edit-title", Label).update(
                f"reasons written to {len(written)} rust2rpm.toml; the next step is regen"
            )

        def _show(
            self,
            name: str,
            prefill: list[str] | None = None,
            flags: list[str] | None = None,
            values: dict[str, str] | None = None,
        ) -> None:
            self._cancel_proc()
            self._cmd = name
            # the result views belong to the previous command: start from the log
            self.query_one("#tabs", Tabs).active = "log"
            self._prefill = set(prefill or [])
            self._prefill_flags = set(flags or [])
            # the playbook's carry list keeps what the previous run established
            # (the COPR project, the target chroot); an explicit jump wins
            pb = tui_playbooks.TUI_PLAYBOOKS.get(name)
            carried = (
                {k: v for k in pb.carry if (v := self._last_form_values.get(k))}
                if pb
                else {}
            )
            self._prefill_values = {**carried, **(values or {})}
            help_text, fields, controls = commands[name]
            self.query_one("#cmd-help", Static).update(Text(f"{name} — {help_text}"))

            async def rebuild() -> None:
                # form ids are namespaced per generation: the previous form's widgets
                # stay registered until their async teardown completes, so ids must not
                # repeat; and mounts are awaited so widgets are ready to use at once
                self._gen += 1
                crates_pane = self.query_one("#form-crates", Vertical)
                options_pane = self.query_one("#form-options", Vertical)
                await crates_pane.remove_children()
                await options_pane.remove_children()
                self._fields, self._widgets = fields, {}
                self._crate_boxes, self._crate_extra = {}, None
                self._selects, self._choice_inputs = {}, {}
                self._multi_rows, self._multi_next = {}, collections.Counter()
                has_crates = has_options = False
                for c in controls:
                    if c.kind == "crates":
                        await self._render_crates(crates_pane, c)
                        has_crates = True
                    elif c.kind == "choice":
                        await self._render_choice(options_pane, c)
                        has_options = True
                    elif c.kind == "multi":
                        await self._render_multi(options_pane, c)
                        has_options = True
                    else:
                        await self._render_field(options_pane, c.fields[0])
                        has_options = True
                crates_pane.display = has_crates
                options_pane.display = has_options
                self._update_preview()

            self._rebuild = self.run_worker(rebuild(), exclusive=True, group="form")

        def _wid(self, key: str) -> str:
            return f"z{self._gen}-{key}"

        async def _render_field(self, form: Vertical, f: tui_form.TuiField) -> None:
            widget_id = self._wid(f"f-{f.key}")
            if f.kind == "flag":
                widget = Checkbox(
                    self._option_label(f),
                    value=f.key in self._prefill_flags,
                    compact=True,
                    id=widget_id,
                    tooltip=f.help or None,
                )
                await form.mount(widget)
            else:
                hint = f.metavar + (" (required)" if f.required else "")
                if f.help:
                    hint += " — " + textwrap.shorten(f.help, width=44, placeholder=" …")
                widget = Input(
                    value=self._prefill_values.get(f.key, f.default),
                    placeholder=hint,
                    id=widget_id,
                    type="number" if f.kind == "number" else "text",
                )
                await form.mount(Label(f.label, classes="field-label"), widget)
            self._widgets[f.key] = widget

        @staticmethod
        def _option_label(f: tui_form.TuiField) -> Text:
            """A flag as a readable row like the crate list: label plus short help."""
            label = Text(f.label)
            if f.help:
                label.append(
                    "  " + textwrap.shorten(f.help, width=44, placeholder=" …"),
                    style="dim",
                )
            return label

        async def _render_crates(self, form: Vertical, c: tui_form.TuiControl) -> None:
            """The with_sel pattern: pick packages from the tree, name new crates, or take all."""
            crates_field = c.fields[0]
            pkgs = packages.local_packages(self.root)
            if pkgs:
                await form.mount(
                    Label("packages under the root", classes="field-label")
                )
                for crate in sorted(pkgs):
                    box = Checkbox(
                        f"{crate}  {pkgs[crate].version or ''}",
                        value=crate in self._prefill,
                        compact=True,
                        id=self._wid(f"pk-{re.sub(r'[^A-Za-z0-9_-]', '_', crate)}"),
                    )
                    await form.mount(box)
                    self._crate_boxes[crate] = box
            hint = crates_field.metavar + " not in the tree; space-separated"
            if crates_field.required:
                hint += " (required)"
            # crates a jump names that are not in the tree yet arrive as names to create
            extra = Input(
                value=" ".join(sorted(self._prefill - set(pkgs))),
                placeholder=hint,
                id=self._wid("f-crates-extra"),
            )
            await form.mount(Label("other crates", classes="field-label"), extra)
            self._crate_extra = extra
            all_field = next((f for f in c.fields if f.dest == "all"), None)
            if all_field is not None:
                box = Checkbox(
                    self._option_label(all_field),
                    value=all_field.key in self._prefill_flags,
                    compact=True,
                    id=self._wid(f"f-{all_field.key}"),
                    tooltip=all_field.help or None,
                )
                await form.mount(box)
                self._widgets[all_field.key] = box

        async def _render_choice(self, form: Vertical, c: tui_form.TuiControl) -> None:
            """A mutually exclusive group: one mode, its value input appears when needed."""
            gi = c.fields[0].group
            select = Select(
                [(f.label, f.key) for f in c.fields],
                allow_blank=True,
                prompt=" / ".join(f.label for f in c.fields),
                id=self._wid(f"g-{gi}"),
                tooltip="mutually exclusive options",
            )
            holder = Vertical(id=self._wid(f"c-{gi}"))
            await form.mount(
                Label(
                    "choose one of: " + "  ".join(f.label for f in c.fields),
                    classes="field-label",
                ),
                select,
                holder,
            )
            self._selects[gi] = select
            for f in c.fields:
                if f.key in self._prefill_flags or f.key in self._prefill_values:
                    select.value = (
                        f.key
                    )  # posts Select.Changed: its input mounts prefilled
                    break

        async def _render_multi(self, form: Vertical, c: tui_form.TuiControl) -> None:
            """A repeatable option: rows of inputs with add/remove."""
            f = c.fields[0]
            # a positional repeatable argument has no option string to name
            again = f.options[0] if f.options else f.metavar
            hint = f.metavar + (" (required)" if f.required else "")
            if f.help:
                hint += " — " + textwrap.shorten(f.help, width=110, placeholder=" …")
            # the label and its '+' share one row: a Button on a row of its own is
            # three tall and lands on top of the inputs below it
            holder = Vertical(id=self._wid(f"m-{f.key}"), classes="multi-rows")
            self._multi_rows[f.key] = []
            self._multi_next[f.key] = 0
            add = Button(
                "+",
                id=self._wid(f"add-{f.key}"),
                classes="multi-add",
                tooltip=f"add another {again}",
            )
            # the row itself is what a keyboard user wants to reach first; the '+'
            # is a mouse affordance and would otherwise be the first Tab stop
            add.can_focus = False
            await form.mount(
                Horizontal(
                    Label(f"{f.label} (repeatable)", classes="field-label"),
                    add,
                    classes="multi-head",
                ),
                holder,
            )
            for v in self._prefill_values.get(f.key, "").split() or [""]:
                await self._add_multi_row(holder, f, hint, v)

        async def _add_multi_row(
            self, holder: Vertical, f: tui_form.TuiField, hint: str, value: str = ""
        ):
            n = self._multi_next[f.key]
            self._multi_next[f.key] += 1
            widget = Input(
                value=value, placeholder=hint, id=self._wid(f"f-{f.key}-{n}")
            )
            await holder.mount(
                Horizontal(
                    widget,
                    Button("–", id=self._wid(f"rm-{f.key}-{n}"), classes="multi-remove"),
                    classes="multi-row",
                )
            )
            self._multi_rows[f.key].append(widget)
            self._set_rows_height(f.key)
            return widget

        def _rows_holder(self, key: str) -> Vertical:
            return self.query_one(f"#{self._wid(f'm-{key}')}", Vertical)

        def _set_rows_height(self, key: str) -> None:
            """Size a row holder by its rows: an 'auto' holder is squeezed to nothing
            by the capped form, which is what hid the inputs behind the buttons."""
            self._rows_holder(key).styles.height = str(
                len(self._multi_rows.get(key, [])) * FORM_ROW_HEIGHT
            )

        def _values(self) -> dict:
            out = {}
            for f in self._fields:
                if f.positional and f.dest == "crates":
                    names = [
                        crate for crate, box in self._crate_boxes.items() if box.value
                    ]
                    extra = self._crate_extra.value.split() if self._crate_extra else []
                    out[f.key] = " ".join(names + extra)
                elif f.group >= 0:
                    select = self._selects.get(f.group)
                    chosen = select.value if select is not None else None
                    if f.key != chosen:
                        out[f.key] = False if f.kind == "flag" else ""
                    elif f.kind == "flag":
                        out[f.key] = True
                    else:
                        widget = self._choice_inputs.get(f.group)
                        out[f.key] = widget.value if widget is not None else ""
                elif f.kind == "multi":  # repeatable option, or repeatable positional
                    out[f.key] = " ".join(
                        r.value.strip()
                        for r in self._multi_rows.get(f.key, [])
                        if r.value.strip()
                    )
                else:
                    widget = self._widgets.get(f.key)
                    if widget is not None:
                        out[f.key] = widget.value
            return out

        def _argv(self) -> list[str]:
            return tui_form.tui_argv(self._fields, self._values())

        def _update_preview(self) -> None:
            values = self._values()
            argv = [
                "rust-deps",
                "--root",
                str(self.root),
                self._cmd or "",
            ] + tui_form.tui_argv(self._fields, values)
            missing = tui_form.tui_missing_required(self._fields, values)
            text = " ".join(shlex.quote(a) for a in argv)
            if missing:
                text += "   —   needs: " + ", ".join(missing)
            self.query_one("#preview", Static).update(Text(text))
            self.query_one("#run", Button).disabled = (
                bool(missing) or self._cmd_running or not self._cmd
            )

        def on_input_changed(self, event: Input.Changed) -> None:
            self._update_preview()

        def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
            self._update_preview()

        async def on_select_changed(self, event: Select.Changed) -> None:
            gi = int(str(event.select.id).rpartition("g-")[2])
            holder = self.query_one(f"#{self._wid(f'c-{gi}')}", Vertical)
            await holder.remove_children()
            self._choice_inputs.pop(gi, None)
            key = event.select.value
            if key:
                f = next(m for m in self._fields if m.key == key)
                if f.kind in ("text", "number"):
                    widget = Input(
                        value=self._prefill_values.get(f.key, f.default),
                        placeholder=f.metavar,
                        id=self._wid(f"f-{f.key}"),
                        type="number" if f.kind == "number" else "text",
                    )
                    await holder.mount(widget)
                    self._choice_inputs[gi] = widget
                    holder.styles.height = str(FORM_ROW_HEIGHT)
                else:
                    holder.styles.height = "0"
            else:
                holder.styles.height = "0"
            self._update_preview()

        async def on_button_pressed(self, event: Button.Pressed) -> None:
            bid = event.button.id or ""
            if bid == "run":
                self.action_run_command()
            elif bid == "cancel":
                self.action_cancel_command()
            elif bid == "tests-save":
                self._save_tests_reasons()
            elif bid == "tests-back":
                self._refresh_overview()
                self.query_one("#screens", ContentSwitcher).current = "overview"
            elif "-add-" in bid:
                key = bid.partition("-add-")[2]
                f = next(m for m in self._fields if m.key == key)
                widget = await self._add_multi_row(
                    self._rows_holder(key), f, f.metavar
                )
                widget.focus()
                self._update_preview()
            elif "-rm-" in bid:
                key, _, n = bid.partition("-rm-")[2].rpartition("-")
                rows = self._multi_rows.get(key) or []
                if len(rows) > 1:
                    widget = next(r for r in rows if r.id == self._wid(f"f-{key}-{n}"))
                    await widget.parent.remove()
                    rows.remove(widget)
                    self._set_rows_height(key)
                    self._update_preview()

        def action_run_command(self) -> None:
            if not self._cmd or tui_form.tui_missing_required(
                self._fields, self._values()
            ):
                return
            values = self._values()
            argv = [
                sys.executable,
                str(config.SELF),
                "--root",
                str(self.root),
                self._cmd,
                *tui_form.tui_argv(self._fields, values),
            ]
            # the playbook carry lists read from here when the next command opens
            self._last_form_values = {
                k: v for k, v in values.items() if isinstance(v, str) and v.strip()
            }
            log = self.query_one("#log", RichLog)
            log.clear()
            self.query_one("#result-table", DataTable).clear()
            self._result_actions = []
            self._fill_actions(self.query_one("#result-actions", OptionList), [])
            self.query_one("#result-actions-label", Label).update("")
            self._stdout = []
            self._cmd_running = True
            self.query_one("#run", Button).disabled = True
            self.query_one("#cancel", Button).disabled = False
            # the run streams into the Log view; _render_structured switches to
            # the Table tab only if the output has a table shape
            self.query_one("#tabs", Tabs).active = "log"
            log.write(Text("$ " + " ".join(shlex.quote(a) for a in argv), style="dim"))
            self.run_command(argv)

        def action_cancel_command(self) -> None:
            self._cancel_proc()

        def _cancel_proc(self) -> None:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()

        # A thread worker with subprocess.Popen: Textual's terminal driver reaps
        # child processes itself, which would leave an asyncio subprocess
        # proc.wait() hanging on a zombie.  Its own exclusive group: switching
        # commands (the form rebuild worker) must not cancel it mid-run.
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
                self._proc = proc

                def drain(pipe, is_stderr: bool) -> None:
                    for raw in pipe:
                        line = raw.rstrip("\r\n")
                        if not is_stderr:
                            self._stdout.append(line)
                        self.call_from_thread(
                            self._write_line, line, tui_line_style(line)
                        )
                    pipe.close()

                err = threading.Thread(
                    target=drain, args=(proc.stderr, True), daemon=True
                )
                err.start()
                drain(proc.stdout, False)
                err.join()
                code = proc.wait()
            except Exception as exc:
                self.call_from_thread(self._write_line, f"ERROR: {exc}", "bold red")
            finally:
                self._proc = None
            self.call_from_thread(self._finish_run, code)

        def _write_line(self, line: str, style: str) -> None:
            self.query_one("#log", RichLog).write(Text(line, style=style))

        def _finish_run(self, code: int) -> None:
            self._cmd_running = False
            self.query_one("#cancel", Button).disabled = True
            self._write_line(f"exit {code}", "bold red" if code else "dim")
            self._render_structured()
            if (
                self._cmd == "doctor"
            ):  # this run just probed the environment: gate on it
                self._apply_tool_gates(tui_results.parse_tool_states(self._stdout))
            self._update_preview()
            self._refresh_overview()  # the tree just changed; keep the cockpit current

        def _json_checked(self) -> bool:
            widget = self._widgets.get("json")
            return bool(widget is not None and widget.value)

        def _render_structured(self) -> None:
            parsed: tuple[list[str], list[list[str]]] | None = None
            actions: list[tuple[str, list[str], list[str], dict[str, str], str]] = []
            if self._json_checked():
                try:
                    parsed = tui_results.parse_json_table(
                        json.loads("\n".join(self._stdout))
                    )
                except json.JSONDecodeError:
                    parsed = None
            else:
                pb = tui_playbooks.TUI_PLAYBOOKS.get(self._cmd or "")
                if pb and pb.result in tui_playbooks.TUI_RESULT_RENDERERS:
                    parsed, actions = tui_playbooks.TUI_RESULT_RENDERERS[pb.result](
                        self._stdout, self.root
                    )
            # steps the tools propose in their own wording become jumps too
            known = {
                (a[0], tuple(a[1]), tuple(a[2]), tuple(sorted(a[3].items())))
                for a in actions
            }
            for a in tui_overview.tui_suggested_actions(
                self._stdout, self.root, commands
            ):
                key = (a[0], tuple(a[1]), tuple(a[2]), tuple(sorted(a[3].items())))
                if key not in known:
                    known.add(key)
                    actions.append(a)
            self._result_actions = actions
            self._fill_actions(self.query_one("#result-actions", OptionList), actions)
            self.query_one("#result-actions-label", Label).update(
                "next steps from this run (select to jump)" if actions else ""
            )
            if parsed is not None:
                headers, rows = parsed
                table = self.query_one("#result-table", DataTable)
                table.clear()
                table.add_columns(*headers)
                for row in rows:
                    table.add_row(*row)
                table.cursor_type = "row"
            elif actions:  # only the proposed steps are worth a pane of their own
                self.query_one("#result-table", DataTable).clear()
            else:  # nothing structured: the output is in the Log view
                self.query_one("#tabs", Tabs).active = "log"
                return
            self.query_one("#tabs", Tabs).active = "table"

        def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
            if event.tab is not None:
                self.query_one("#view", ContentSwitcher).current = event.tab.id

    return RustDepsTUI


def cmd_tui(args) -> None:
    try:
        app = make_tui_app()
    except ImportError:
        util.die("rust-deps tui needs python3-textual: dnf install python3-textual")
    app(args.root).run()
