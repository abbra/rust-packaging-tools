"""rust-deps crate archives: reading members, manifests, feature closure."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import tarfile

from . import packages
from . import util


def read_crate_member(crate_file: Path, member: str) -> str | None:
    with tarfile.open(crate_file) as tf:
        top = tf.getnames()[0].split("/")[0]
        try:
            f = tf.extractfile(f"{top}/{member}")
        except KeyError:
            return None
        return f.read().decode() if f else None


def crate_members(crate_file: Path) -> list[str]:
    with tarfile.open(crate_file) as tf:
        return [n.split("/", 1)[1] for n in tf.getnames() if "/" in n]


def extract_patched(pkg: packages.LocalPackage, dest_root: Path) -> Path:
    """Extract the crate and apply its spec patches, like %autosetup -p1."""
    assert (
        pkg.crate_file and pkg.crate_file.exists()
    ), f"{pkg.crate}: .crate missing, run regen"
    dest = dest_root / f"{pkg.crate}-{pkg.version}"
    shutil.rmtree(dest, ignore_errors=True)
    dest_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(pkg.crate_file) as tf:
        tf.extractall(dest_root, filter="data")
    for patch in spec_patches(pkg):
        res = util.run(
            ["patch", "-s", "-p1", "-i", str(patch)], cwd=dest, capture_output=True
        )
        if res.returncode != 0:
            util.die(f"{patch.name} does not apply:\n{res.stdout}{res.stderr}")
    return dest


def spec_patches(pkg: packages.LocalPackage) -> list[Path]:
    text = pkg.spec.read_text()
    return [pkg.dir / m for m in re.findall(r"^Patch\d*:\s+(\S+)", text, re.M)]


def manifest_deps(toml: dict) -> list[dict]:
    """Dependencies of a Cargo.toml as crates.io-API-shaped dicts."""
    out = []

    def add(table, kind, target=None):
        for key, spec in (table or {}).items():
            if isinstance(spec, str):
                spec = {"version": spec}
            out.append(
                {
                    "name": key,
                    "crate_id": spec.get("package", key),
                    "req": spec.get("version", "*"),
                    "optional": spec.get("optional", False),
                    "default_features": spec.get("default-features", True),
                    "features": spec.get("features", []),
                    "kind": kind,
                    "target": target,
                    "path": spec.get("path"),
                    "workspace": bool(spec.get("workspace")),
                    "git": spec.get("git"),
                    "registry": spec.get("registry"),
                }
            )

    for kind, key in (
        ("normal", "dependencies"),
        ("dev", "dev-dependencies"),
        ("build", "build-dependencies"),
    ):
        add(toml.get(key), kind)
        for tgt, tt in toml.get("target", {}).items():
            add(tt.get(key), kind, tgt)
    return out


def feature_closure(
    features: dict[str, list[str]], start: set[str]
) -> tuple[set[str], set[str], dict[str, set[str]]]:
    """Enabled features, enabled optional dependencies, and the features each
    dependency is enabled with: 'default = ["serde/derive"]' enables 'derive'
    on serde even though the dependency itself does not list it."""
    feats, deps = set(), set()
    dep_features: dict[str, set[str]] = {}
    stack = list(start)
    while stack:
        f = stack.pop()
        if f in feats:
            continue
        feats.add(f)
        for item in features.get(f, []):
            if item.startswith("dep:"):
                deps.add(item[4:])
            elif "/" in item:
                dep, feat = item.split("/", 1)
                dep_features.setdefault(dep.rstrip("?"), set()).add(feat)
                if not dep.endswith("?"):
                    deps.add(dep)
            else:
                stack.append(item)
                deps.add(item)  # implicit feature of an optional dependency
    return feats, deps, dep_features
