"""rust-deps Cargo.toml edits: cargo-toml-edits.toml to fix-metadata diffs."""

from __future__ import annotations

from pathlib import Path
import json
import os
import re
import tomllib

from . import config
from . import util

EDIT_KEYS = {
    "drop-dev-dependencies": list,
    "drop-dependencies": list,
    "drop-features": list,
    "add-default-features": list,
    "drop-targets": list,
    "set-version": dict,
    "set-dev-version": dict,
    "add-dev-dependencies": dict,
}


def load_edits(path: Path) -> dict:
    if not path.exists():
        return {}
    edits = tomllib.loads(path.read_text())
    for k, v in edits.items():
        if k not in EDIT_KEYS:
            util.die(f"{path}: unknown key '{k}' (known: {', '.join(EDIT_KEYS)})")
        if not isinstance(v, EDIT_KEYS[k]):
            util.die(f"{path}: '{k}' must be a {EDIT_KEYS[k].__name__}")
    return {k: v for k, v in edits.items() if v}


def apply_edits(text: str, edits: dict) -> str:
    """Apply declarative edits to a normalized (cargo package) Cargo.toml."""
    drop_dev = set(edits.get("drop-dev-dependencies", []))
    drop_dep = set(edits.get("drop-dependencies", []))
    drop_feat = set(edits.get("drop-features", []))
    drop_targets = set(edits.get("drop-targets", []))
    set_ver = edits.get("set-version", {})
    set_dev_ver = edits.get("set-dev-version", {})

    # features may refer to dev-dependencies; drop those references too, unless
    # the crate also has a regular dependency of that name
    regular = set(
        re.findall(
            r"(?m)^\[(?:target\.(?:'[^']*'|\"[^\"]*\")\.)?(?:dependencies|build-dependencies)\.\"?([^\]\"]+)",
            text,
        )
    )
    feature_refs = drop_dep | (drop_dev - regular)

    # dropped features stay in the code as #[cfg(feature = "...")]: declare them
    # as expected cfg values, or rustc warns about each use (unexpected_cfgs)
    orig = tomllib.loads(text)
    dropped = sorted(drop_feat & set(orig.get("features", {})))
    lint = orig.get("lints", {}).get("rust", {}).get("unexpected_cfgs")
    lint = {"level": lint} if isinstance(lint, str) else dict(lint or {"level": "warn"})
    declare = bool(dropped) and lint.get("level") != "allow"

    out = []
    for block in re.split(r"\n(?=\[)", text):
        header = block.split("\n", 1)[0].strip()
        if declare and header == "[lints.rust.unexpected_cfgs]":
            continue  # written again below, with the dropped features
        if declare and header == "[lints.rust]":
            block = re.sub(r"(?m)^unexpected_cfgs\s*=.*\n?", "", block)
        m = re.match(
            r"""^\[(?:target\.(?:'[^']*'|"[^"]*")\.)?(dev-dependencies|dependencies|build-dependencies)\.([^\]]+)\]$""",
            header,
        )
        if m:
            kind, name = m.groups()
            name = name.strip('"')
            if kind == "dev-dependencies":
                if name in drop_dev:
                    continue
                if name in set_dev_ver:
                    block = re.sub(
                        r'(?m)^version = "[^"]*"',
                        f'version = "{set_dev_ver[name]}"',
                        block,
                    )
            else:
                if name in drop_dep:
                    continue
                if name in set_ver:
                    block = re.sub(
                        r'(?m)^version = "[^"]*"', f'version = "{set_ver[name]}"', block
                    )
        m = re.match(r"^\[\[(test|bench|example|bin)\]\]$", header)
        if m:
            nm = re.search(r'(?m)^name = "([^"]*)"', block)
            if nm and f"{m.group(1)}:{nm.group(1)}" in drop_targets:
                continue
        if header == "[features]":
            block = _edit_features(
                block, drop_feat, feature_refs, edits.get("add-default-features", [])
            )
        out.append(block)
    text = "\n".join(out)

    if add_dev := edits.get("add-dev-dependencies"):
        text = text.rstrip("\n") + "\n"
        for (
            name,
            spec,
        ) in add_dev.items():  # "1.0", or {version = "1.0", features = [...]}
            spec = {"version": spec} if isinstance(spec, str) else spec
            text += f'\n[dev-dependencies.{name}]\nversion = {json.dumps(spec["version"])}\n'
            if spec.get("features"):
                text += f"features = {json.dumps(spec['features'])}\n"
            if spec.get("default-features") is False:
                text += "default-features = false\n"
    if declare:
        values = ", ".join(json.dumps(f) for f in dropped)
        checks = [json.dumps(c) for c in lint.get("check-cfg", [])] + [
            f"'cfg(feature, values({values}))'"
        ]
        text = (
            text.rstrip("\n")
            + "\n\n[lints.rust.unexpected_cfgs]\n"
            + "".join(
                f"{k} = {json.dumps(v)}\n" for k, v in lint.items() if k != "check-cfg"
            )
        )
        text += "check-cfg = [\n" + "".join(f"    {c},\n" for c in checks) + "]\n"
    return re.sub(r"\n{3,}", "\n\n", text)


def _edit_features(block: str, drop_feat: set, drop_dep: set, add_default: list) -> str:
    res, skipping = [], False
    for ln in block.split("\n"):
        fm = re.match(r'^"?([A-Za-z0-9_.+-]+)"? = (\[.*)$', ln)
        if fm and fm.group(1) in drop_feat:
            skipping = not fm.group(2).rstrip().endswith("]")
            continue
        if skipping:
            if ln.startswith("]"):
                skipping = False
            continue
        res.append(ln)
    block = "\n".join(res)
    for name in drop_feat | drop_dep:
        ref = r'"(?:dep:)?' + re.escape(name) + r'\??(?:/[^"]*)?"'
        block = re.sub(r"\n[ \t]*" + ref + r",", "", block)  # multi-line list entry
        block = re.sub(ref + r",?[ \t]*", "", block)  # single-line list entry
    block = re.sub(r"\[\s*\]", "[]", block)
    for f in add_default:
        if re.search(r"(?m)^default = \[\]", block):
            block = re.sub(r"(?m)^default = \[\]", f'default = ["{f}"]', block, count=1)
        elif re.search(r"(?m)^default = \[", block):
            block = re.sub(
                r"(?m)^default = \[", f'default = [\n    "{f}",', block, count=1
            )
        else:
            # no explicit default: cargo's implicit default enables no optional crate,
            # so declare a default list to enable the feature
            block = re.sub(
                r"(?m)^\[features\].*\n",
                lambda m: m.group(0) + f'default = ["{f}"]\n',
                block,
                count=1,
            )
    return block


def editor_mode(toml_path: str) -> None:
    """Invoked by rust2rpm as $EDITOR: apply the edits file non-interactively."""
    edits = load_edits(Path(os.environ[config.EDITOR_ENV]))
    p = Path(toml_path)
    p.write_text(apply_edits(p.read_text(), edits))


CHECK_CFG_COMMENT = (
    "declare the dropped features as expected cfg values (lints.rust.unexpected_cfgs), "
    "so rustc does not warn about the code that still tests them"
)


def describe_edits(edits: dict) -> list[str]:
    """Default cargo-toml-patch-comments for a set of edits."""
    out = []
    if v := edits.get("drop-dev-dependencies"):
        out.append(f"drop dev-dependencies not packaged in Fedora: {', '.join(v)}")
    if v := edits.get("drop-dependencies"):
        out.append(f"drop optional dependencies not packaged in Fedora: {', '.join(v)}")
    if v := edits.get("drop-features"):
        out.append(f"drop features that need unpackaged crates: {', '.join(v)}")
        out.append(CHECK_CFG_COMMENT)
    if v := edits.get("add-default-features"):
        out.append(f"enable by default instead: {', '.join(v)}")
    if v := edits.get("set-version"):
        out.append(
            "adjust dependency versions to Fedora: "
            + ", ".join(f"{k} {r}" for k, r in v.items())
        )
    if v := edits.get("set-dev-version"):
        out.append(
            "adjust dev-dependency versions to Fedora: "
            + ", ".join(f"{k} {r}" for k, r in v.items())
        )
    if v := edits.get("add-dev-dependencies"):
        out.append("restore dev-dependencies needed by tests: " + ", ".join(v))
    if v := edits.get("drop-targets"):
        out.append(f"drop targets: {', '.join(v)}")
    return out
