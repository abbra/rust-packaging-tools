"""rust-deps TUI forms: widgets derived from the argparse parser."""

from __future__ import annotations

from dataclasses import dataclass, field
import argparse
import collections
import os
import re


@dataclass
class TuiField:
    """One form control, derived from an argparse action."""

    kind: str  # "text" | "multi" | "number" | "flag"
    dest: str
    key: str  # unique per form: 'regen' has --compat and --no-compat sharing a dest
    options: list[str]  # the option string to pass; empty for positionals
    label: str
    metavar: str
    help: str
    required: bool
    default: str
    positional: bool
    group: int = -1  # index of its mutually exclusive group, -1 for none


@dataclass
class TuiControl:
    """A rendered unit of a form, shaped by what the parser says the options are:
    'crates' = the positional selection plus --all (the with_sel pattern);
    'choice' = one mutually exclusive group; 'multi' = a repeatable option;
    'field' = a single option."""

    kind: str
    fields: list[TuiField]


def tui_option(action: argparse.Action) -> str:
    """The option string the UI passes: the long form when there is one."""
    return next(
        (o for o in action.option_strings if o.startswith("--")),
        action.option_strings[0],
    )


def tui_option_strings(action: argparse.Action) -> list[str]:
    """The canonical option first (what the form passes), then its aliases: the
    tools' own command lines may name the short form ('-r' beside '--chroot')."""
    canonical = tui_option(action)
    return [canonical] + [o for o in action.option_strings if o != canonical]


def tui_fields(parser: argparse.ArgumentParser) -> list[TuiField]:
    """The form of one subcommand, generated from its argparse actions."""
    fields: list[TuiField] = []
    seen: collections.Counter = collections.Counter()
    groups = {
        id(a): gi
        for gi, g in enumerate(parser._mutually_exclusive_groups)
        for a in g._group_actions
    }
    for action in parser._actions:
        if action.dest == "help" or isinstance(action, argparse._SubParsersAction):
            continue
        seen[action.dest] += 1
        key = (
            action.dest
            if seen[action.dest] == 1
            else f"{action.dest}-{seen[action.dest]}"
        )
        metavar = action.metavar or action.dest.replace("_", "-").upper()
        label = "/".join(action.option_strings) or metavar
        default = "" if action.default in (None, [], False) else str(action.default)
        help_text = (action.help or "").replace(
            "%(default)s", default or str(action.default)
        )
        cls = type(action).__name__
        group = groups.get(id(action), -1)
        if not action.option_strings:  # positional
            kind = "multi" if action.nargs in ("*", "+") else "text"
            fields.append(
                TuiField(
                    kind,
                    action.dest,
                    key,
                    [],
                    metavar,
                    metavar,
                    help_text,
                    action.nargs == "+",
                    "",
                    True,
                    group,
                )
            )
        elif cls in ("_StoreTrueAction", "_StoreFalseAction"):
            fields.append(
                TuiField(
                    "flag",
                    action.dest,
                    key,
                    tui_option_strings(action),
                    label,
                    "",
                    help_text,
                    False,
                    "",
                    False,
                    group,
                )
            )
        elif cls == "_AppendAction":
            fields.append(
                TuiField(
                    "multi",
                    action.dest,
                    key,
                    tui_option_strings(action),
                    label,
                    metavar,
                    help_text,
                    action.required,
                    "",
                    False,
                    group,
                )
            )
        elif action.type is int:
            fields.append(
                TuiField(
                    "number",
                    action.dest,
                    key,
                    tui_option_strings(action),
                    label,
                    metavar,
                    help_text,
                    action.required,
                    default,
                    False,
                    group,
                )
            )
        else:
            fields.append(
                TuiField(
                    "text",
                    action.dest,
                    key,
                    tui_option_strings(action),
                    label,
                    metavar,
                    help_text,
                    action.required,
                    default,
                    False,
                    group,
                )
            )
    return fields


def tui_controls(parser: argparse.ArgumentParser) -> list[TuiControl]:
    """Group the fields into controls that match the parser's own structure."""
    fields = tui_fields(parser)
    controls: list[TuiControl] = []
    absorbed: set[str] = set()
    for i, f in enumerate(fields):
        if f.key in absorbed:
            continue
        if (
            f.positional and f.dest == "crates"
        ):  # with_sel: the crate picker absorbs --all
            members = [f]
            nxt = fields[i + 1] if i + 1 < len(fields) else None
            if nxt is not None and nxt.kind == "flag" and nxt.dest == "all":
                members.append(nxt)
                absorbed.add(nxt.key)
            controls.append(TuiControl("crates", members))
        elif f.group >= 0:
            members = [later for later in fields[i:] if later.group == f.group]
            absorbed.update(m.key for m in members)
            controls.append(TuiControl("choice", members))
        elif f.kind == "multi":
            controls.append(TuiControl("multi", [f]))
        else:
            controls.append(TuiControl("field", [f]))
    return controls


def tui_missing_required(fields: list[TuiField], values: dict) -> list[str]:
    """Labels of required arguments the form does not have yet."""
    missing = []
    for f in fields:
        if not f.required:
            continue
        v = values.get(f.key)
        if isinstance(v, (list, tuple)):
            if not any(str(p).strip() for p in v):
                missing.append(f.label)
        elif (not v) or (isinstance(v, str) and not v.split()):
            missing.append(f.label)
    return missing


def control_is_argument(c: TuiControl) -> bool:
    """Whether a control names *what the command acts on* rather than *how to run it*.

    The crate picker and anything positional (workspace paths, a crate name) is an
    argument and belongs in the form's argument column.  Anything declared with an
    option string is an option, even when it takes a value: it belongs to the
    options column, next to the rest of the switches.
    """
    return c.kind == "crates" or bool(c.fields and c.fields[0].positional)


def tui_argv(fields: list[TuiField], values: dict) -> list[str]:
    """Turn form values into command-line arguments; empty fields are left out.

    A repeatable field carries its rows as a list, so a value with whitespace
    in it (a directory whose name has a space) stays one argument; a joined
    string is split on whitespace for the callers that build one (the crate
    picker, a playbook carry-over).
    """
    argv: list[str] = []
    for f in fields:
        v = values.get(f.key)
        if f.kind == "flag":
            if v:
                argv.append(f.options[0])
        elif f.kind == "multi":
            parts = list(v) if isinstance(v, (list, tuple)) else str(v or "").split()
            for part in parts:
                if not part:
                    continue
                if f.positional:
                    argv.append(expand_user_path(part))
                else:
                    argv.extend((f.options[0], expand_user_path(part)))
        elif v not in (None, ""):
            if not f.positional:
                argv.append(f.options[0])
            argv.append(expand_user_path(str(v)))
    return argv


# '~' only means a home directory when it is the whole value or the first path
# component: a version requirement ('~1.0') or a crate request ('serde@~1.0')
# starts the same way and must stay exactly as typed.
_TILDE_PATH = re.compile(r"^~(?:$|/)|^~[^/]+/")


def expand_user_path(value: str) -> str:
    """Expand a '~' the way a shell would.

    A form passes its values straight to a 'rust-deps' subprocess, with no shell
    between them, so a '~' typed into an input would otherwise reach the command
    literally and no directory would match it.
    """
    if not _TILDE_PATH.match(value):
        return value
    try:
        return os.path.expanduser(value)
    except KeyError:  # '~someone' that is not an account on this machine
        return value
