"""Unit tests for rust-deps: offline, no rust2rpm runs, no network.

rust-deps is the rust_deps package under fedora-rust-packaging/scripts/;
import it straight from the checkout.
"""

import argparse
import asyncio
import ast
import json
import subprocess
import sys
import tomllib
import urllib.error
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fedora-rust-packaging" / "scripts"))
import rust_deps as rd


# ─── versions and targets ───────────────────────────────────────────────────

@pytest.mark.parametrize("req, version, expected", [
    ("^1.2", "1.9.0", True),
    ("^1.2", "2.0.0", False),
    (">=0.2.2, <0.3.0", "0.2.2", True),
    (">=0.2.2, <0.3.0", "0.3.0", False),
    ("=0.3.6", "0.3.6", True),
    ("*", "not-a-version", False),
])
def test_req_matches(req, version, expected):
    assert rd.versions.req_matches(req, version) is expected


@pytest.mark.parametrize("target, foreign", [
    (None, False),
    ('cfg(windows)', True),
    ('cfg(target_os = "macos")', True),
    ('cfg(unix)', False),
    ('cfg(not(windows))', False),
    ('cfg(target_os = "linux")', False),
    ("x86_64-pc-windows-msvc", True),
    ("x86_64-unknown-linux-gnu", False),
])
def test_is_foreign(target, foreign):
    assert rd.versions.is_foreign(target) is foreign


def test_feature_closure():
    features = {"default": ["std", "derive"], "std": [], "derive": ["dep:synta-derive", "serde?/derive"],
                "tls": ["rustls/ring"]}
    feats, deps, dep_features = rd.archives.feature_closure(features, {"default"})
    assert feats == {"default", "std", "derive"}
    assert "synta-derive" in deps and "serde" not in deps  # "serde?/derive" does not enable serde
    # the feature a 'dep/feature' reference enables is reported for that dependency
    assert dep_features == {"serde": {"derive"}}


# ─── chroots ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("chroot, repos", [
    ("fedora-44-x86_64", ["fedora-44", "updates-released-f44"]),
    ("fedora-rawhide-aarch64", ["rawhide"]),
    ("rhel+epel-10-x86_64", ["epel-z-10"]),
    ("rhel+epel-9-x86_64", ["epel-9"]),
    ("centos-stream+epel-10-x86_64", ["epel-10"]),
    ("centos-stream+epel-next-9-x86_64", ["epel-9", "epel-next-9"]),
])
def test_chroot_metalinks(chroot, repos):
    assert rd.fedora.chroot_metalinks(chroot) == repos


def test_chroot_release_and_mock_chroot():
    assert rd.fedora.chroot_release("rhel+epel-10-aarch64") == "rhel+epel-10"
    assert rd.copr.mock_chroot("rhel+epel-10-x86_64") == "centos-stream+epel-10-x86_64"
    assert rd.copr.mock_chroot("fedora-44-x86_64") == "fedora-44-x86_64"


def test_chroot_metalinks_unknown():
    with pytest.raises(SystemExit):
        rd.fedora.chroot_metalinks("opensuse-tumbleweed-x86_64")


# ─── status columns ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("target, chroot", [
    ("fedora-44", "fedora-44-x86_64"),
    ("fedora-rawhide", "fedora-rawhide-x86_64"),
    ("rhel+epel-10", "rhel+epel-10-x86_64"),
    ("epel-10", "epel-10-x86_64"),
    ("fedora-45-aarch64", "fedora-45-aarch64"),
])
def test_release_chroot(target, chroot):
    assert rd.status.release_chroot(target) == chroot


def test_release_chroot_epel_flavours():
    # COPR's rhel+epel-N uses EPEL for the current RHEL minor; plain epel-N is EPEL itself
    assert rd.fedora.chroot_metalinks(rd.status.release_chroot("rhel+epel-10")) == ["epel-z-10"]
    assert rd.fedora.chroot_metalinks(rd.status.release_chroot("epel-10")) == ["epel-10"]


@pytest.mark.parametrize("versions, local, all_versions, cell", [
    ([], "1.0.0", False, "-"),
    (["1.0.21"], "1.0.23", False, "1.0.21"),
    (["0.16.1", "0.17.1"], "0.17.1", False, "0.17.1= (+1)"),
    (["0.16.1", "0.17.1"], "0.16.1", False, "0.17.1 (+1)"),
    (["0.16.1", "0.17.1"], "0.16.1", True, "0.16.1=,0.17.1"),
])
def test_version_cell(versions, local, all_versions, cell):
    assert rd.status.version_cell(versions, local, all_versions) == cell


def test_host_release(tmp_path, monkeypatch):
    osr = tmp_path / "os-release"
    osr.write_text('NAME="Fedora Linux"\nID=fedora\nVERSION_ID=45\n')
    monkeypatch.delenv("RUST_DEPS_DNF_ARGS", raising=False)
    assert rd.status.host_release(osr) == "fedora-45"
    monkeypatch.setenv("RUST_DEPS_DNF_ARGS", "--releasever=rawhide")
    assert rd.status.host_release(osr) == "fedora-45 (--releasever=rawhide)"
    assert rd.status.host_release(tmp_path / "missing") == "host (--releasever=rawhide)"


# ─── Cargo.toml edits ───────────────────────────────────────────────────────

CARGO_TOML = """\
[package]
name = "demo"
version = "1.0.0"

[features]
default = ["std", "tls-aws-lc-rs"]
std = []
magnus = ["dep:magnus"]
tls-aws-lc-rs = ["rustls/aws-lc-rs"]
tls-ring = ["rustls/ring"]
bench = ["criterion"]

[dependencies.magnus]
version = "0.8"
optional = true

[dependencies.rustls]
version = "0.23"

[dependencies.zmij]
version = "1.0.21"

[dev-dependencies.criterion]
version = "0.7"

[dev-dependencies.hex]
version = "0.3"

[lints.rust]
unsafe_code = "warn"
"""


def edit(edits: dict, text: str = CARGO_TOML) -> dict:
    return tomllib.loads(rd.edits.apply_edits(text, edits))


def test_drop_dependency_and_feature():
    t = edit({"drop-dependencies": ["magnus"], "drop-features": ["magnus"]})
    assert "magnus" not in t["dependencies"]
    assert "magnus" not in t["features"]


def test_drop_dev_dependency_removes_feature_references():
    t = edit({"drop-dev-dependencies": ["criterion"]})
    assert "criterion" not in t["dev-dependencies"]
    assert t["features"]["bench"] == []


def test_add_default_feature_replaces_dropped_one():
    t = edit({"drop-features": ["tls-aws-lc-rs"], "add-default-features": ["tls-ring"]})
    assert t["features"]["default"] == ["tls-ring", "std"]


def test_add_default_feature_declares_a_missing_default():
    """Without an explicit default, cargo's implicit default enables no optional crate."""
    text = CARGO_TOML.replace('default = ["std", "tls-aws-lc-rs"]\n', "")
    t = edit({"drop-features": ["tls-aws-lc-rs"], "add-default-features": ["tls-ring"]}, text)
    assert t["features"]["default"] == ["tls-ring"]
    assert "tls-aws-lc-rs" not in t["features"]


def test_set_versions():
    t = edit({"set-version": {"zmij": "1.0.23"}, "set-dev-version": {"hex": "0.4"}})
    assert t["dependencies"]["zmij"]["version"] == "1.0.23"
    assert t["dev-dependencies"]["hex"]["version"] == "0.4"


def test_add_dev_dependency():
    t = edit({"add-dev-dependencies": {"tempfile": "3"}})
    assert t["dev-dependencies"]["tempfile"]["version"] == "3"


def test_dropped_features_become_expected_cfg_values():
    """#[cfg(feature = "magnus")] stays in the code: declare it, keep the crate's own lints."""
    t = edit({"drop-dependencies": ["magnus"], "drop-features": ["magnus", "tls-aws-lc-rs"]})
    lint = t["lints"]["rust"]
    assert lint["unsafe_code"] == "warn"
    assert lint["unexpected_cfgs"]["check-cfg"] == ['cfg(feature, values("magnus", "tls-aws-lc-rs"))']


def test_check_cfg_merges_with_existing_config():
    text = CARGO_TOML.replace('unsafe_code = "warn"\n',
                              'unexpected_cfgs = { level = "deny", priority = 1, check-cfg = ["cfg(docsrs)"] }\n')
    lint = edit({"drop-features": ["magnus"]}, text)["lints"]["rust"]["unexpected_cfgs"]
    assert lint == {"level": "deny", "priority": 1, "check-cfg": ["cfg(docsrs)", 'cfg(feature, values("magnus"))']}


def test_check_cfg_not_added_without_dropped_features():
    assert "unexpected_cfgs" not in edit({"drop-features": ["no-such-feature"]})["lints"]["rust"]


def test_describe_edits_mentions_check_cfg():
    comments = rd.edits.describe_edits({"drop-features": ["magnus"]})
    assert rd.edits.CHECK_CFG_COMMENT in comments


# ─── local packages ─────────────────────────────────────────────────────────

def make_package(root: Path, crate: str, spec_name: str, version: str = "0.17.1") -> Path:
    d = root / crate
    d.mkdir()
    (d / "rust2rpm.toml").write_text("")
    (d / f"{spec_name}.spec").write_text(f"%global crate {crate}\nName: {spec_name}\nVersion:        {version}\n")
    return d


def test_local_packages_regular_and_compat(tmp_path):
    make_package(tmp_path, "zmij", "rust-zmij", "1.0.23")
    make_package(tmp_path, "hashbrown", "rust-hashbrown0.17")
    (tmp_path / "not-a-package").mkdir()
    pkgs = rd.packages.local_packages(tmp_path)
    assert set(pkgs) == {"zmij", "hashbrown"}
    assert not pkgs["zmij"].compat and pkgs["zmij"].rpm_name == "rust-zmij"
    assert pkgs["hashbrown"].compat and pkgs["hashbrown"].spec.name == "rust-hashbrown0.17.spec"
    assert pkgs["hashbrown"].version == "0.17.1"


def test_targets_toml(tmp_path):
    d = make_package(tmp_path, "serde_assert", "rust-serde_assert", "0.8.0")
    (d / rd.config.TARGETS_FILE).write_text('only = ["fedora-44", "rhel+epel-10"]\n')
    pkg = rd.packages.local_packages(tmp_path)["serde_assert"]
    assert pkg.builds_in("fedora-44-aarch64") and pkg.builds_in("rhel+epel-10-x86_64")
    assert not pkg.builds_in("fedora-45-x86_64")


@pytest.mark.parametrize("tests, bad", [
    ({"skip": ["tests::test_read_timeout", "::chain_call", "src/lib.rs"]}, []),
    ({"skip": {"doc": ["src/lib.rs - (line 15)"], "lib": ["tests::ok"]}}, ["src/lib.rs - (line 15)"]),
    ({"skip": {"doc": ["connection::Connection<S>::chain_", "%{name}"]}},
     ["connection::Connection<S>::chain_", "%{name}"]),
    ({}, []),
])
def test_unsafe_skips(tests, bad):
    assert rd.trial.unsafe_skips(tests) == bad


@pytest.mark.parametrize("skip, bad", [
    ("src/lib.rs - (line 15)", ["src/lib.rs - (line 15)"]),
    ("tests::ok", []),
])
def test_unsafe_skips_accepts_a_lone_string(skip, bad):
    assert rd.trial.unsafe_skips({"skip": skip}) == bad


def test_spec_invocations_with_a_string_skip(tmp_path):
    d = make_package(tmp_path, "smol", "rust-smol", "2.0.2")
    (d / "rust2rpm.toml").write_text('[tests]\nskip = "tests::needs_network"\n')
    assert rd.trial.spec_invocations(rd.packages.local_packages(tmp_path)["smol"]) == \
        [("all", ["--", "--skip", "tests::needs_network"])]


def test_trial_refuses_unsafe_skips(tmp_path, capsys):
    d = make_package(tmp_path, "smol", "rust-smol", "2.0.2")
    (d / "rust2rpm.toml").write_text('[tests]\nrun = ["doc"]\nskip.doc = ["src/lib.rs - (line 15)"]\n')
    assert rd.trial.trial(rd.packages.local_packages(tmp_path)["smol"], discover=False) is False
    assert "not shell-safe" in capsys.readouterr().err


def make_crate(tmp_path: Path, files: dict[str, tuple[str, int]]) -> Path:
    """A .crate archive with the given files: path -> (content, mode)."""
    import io
    import tarfile
    crate = tmp_path / "demo-1.0.0.crate"
    with tarfile.open(crate, "w:gz") as tf:
        for name, (content, mode) in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"demo-1.0.0/{name}")
            info.size, info.mode = len(data), mode
            tf.addfile(info, io.BytesIO(data))
    return crate


def test_foreign_shebangs(tmp_path):
    crate = make_crate(tmp_path, {
        "wasm/emscripten/runner.py": ("#!/usr/local/bin/python\n", 0o755),
        "scripts/ok.sh": ("#!/usr/bin/bash\n", 0o755),
        "scripts/env.py": ("#!/usr/bin/env python3\n", 0o755),
        "not-executable.py": ("#!/usr/local/bin/python\n", 0o644),
        "src/lib.rs": ("", 0o644),
    })
    assert rd.regen.foreign_shebangs(crate, {}) == [("wasm/emscripten/runner.py", "/usr/local/bin/python")]
    # removed (or made non-executable) by the prep scripts: handled
    handled = {"scripts": {"prep": {"post": ["rm -r newsfragments/ wasm/"]}}}
    assert rd.regen.foreign_shebangs(crate, handled) == []


# ─── updates to a new upstream release ──────────────────────────────────────

def stub_buildrequires(monkeypatch, brs: dict[str, list[str]]):
    """BuildRequires lines per crate, as build_info would return them."""
    monkeypatch.setattr(rd.copr, "build_info", lambda pkg: ({}, brs.get(pkg.crate, [])))


def crate_dep(name: str, req: str, kind: str = "normal", optional: bool = False) -> dict:
    """A dependency as the crates.io API shapes it, for resolve."""
    return {"name": name, "crate_id": name, "req": req, "optional": optional,
            "default_features": True, "features": [], "kind": kind, "target": None}


def stub_cratesio(monkeypatch, versions: dict[str, list[str]], deps: dict[str, list[dict]]):
    monkeypatch.setattr(rd.cratesio, "crate_versions",
                        lambda name: [{"num": v, "yanked": False} for v in versions.get(name, [])])
    monkeypatch.setattr(rd.cratesio, "crate_dependencies", lambda name, version: deps.get(name, []))


def test_build_stages_orders_by_local_dependencies(tmp_path, monkeypatch):
    for crate in ("leaf", "mid", "top"):
        make_package(tmp_path, crate, f"rust-{crate}")
    stub_buildrequires(monkeypatch, {
        "mid": ["crate(leaf) >= 0.1"],
        "top": ["crate(mid) >= 0.1", "crate(leaf) >= 0.1", "crate(serde) >= 1.0"],  # serde: Fedora has it
    })
    pkgs = list(rd.packages.local_packages(tmp_path).values())
    stages = rd.build.build_stages(pkgs, tmp_path)
    assert [[p.crate for p in s] for s in stages] == [["leaf"], ["mid"], ["top"]]


def test_build_stages_detects_a_cycle(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, "a", "rust-a")
    make_package(tmp_path, "b", "rust-b")
    stub_buildrequires(monkeypatch, {"a": ["crate(b) >= 0.1"], "b": ["crate(a) >= 0.1"]})
    with pytest.raises(SystemExit):
        rd.build.build_stages(list(rd.packages.local_packages(tmp_path).values()), tmp_path)
    assert "dependency cycle" in capsys.readouterr().err


def test_build_stages_warns_about_dependencies_outside_the_selection(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, "a", "rust-a")
    make_package(tmp_path, "b", "rust-b")
    stub_buildrequires(monkeypatch, {"a": ["crate(b) >= 0.1"]})
    stages = rd.build.build_stages([rd.packages.local_packages(tmp_path)["a"]], tmp_path)
    assert [[p.crate for p in s] for s in stages] == [["a"]]
    assert "not in this build: b" in capsys.readouterr().err


def test_resolve_new_with_transitive_deps(monkeypatch):
    stub_cratesio(monkeypatch, {"top": ["2.0.0"], "mid": ["1.5.0"]},
                  {"top": [crate_dep("mid", "^1.0")], "mid": []})
    needed = rd.resolver.resolve([("top", "*", "(test)", ["default"])], rd.fedora.FedoraIndex({}), {})
    assert {k: n.status for k, n in needed.items()} == {"top@2.0.0": "new", "mid@1.5.0": "new"}
    assert needed["mid@1.5.0"].needed_by == {"top"}


def test_resolve_update_when_fedora_has_another_version(monkeypatch):
    stub_cratesio(monkeypatch, {"mid": ["1.5.0"]}, {"mid": []})
    needed = rd.resolver.resolve([("mid", "^1.2", "(test)", ["default"])], rd.fedora.FedoraIndex({"mid": {"1.0.0": {""}}}), {})
    assert needed["mid@1.5.0"].status == "update"
    assert needed["mid@1.5.0"].fedora_versions == ["1.0.0"]


def test_resolve_features_when_fedora_version_lacks_a_feature(monkeypatch):
    fedora = rd.fedora.FedoraIndex({"mid": {"1.5.0": {""}}})  # ships the crate, not the feature
    needed = rd.resolver.resolve([("mid", "^1.2", "(test)", ["derive"])], fedora, {})
    assert needed["mid@1.5.0"].status == "features"
    assert needed["mid@1.5.0"].missing_features == ["derive"]


def test_resolve_counts_a_matching_local_package(tmp_path, monkeypatch):
    stub_cratesio(monkeypatch, {"top": ["2.0.0"]}, {"top": [crate_dep("mid", "^1.0")]})
    make_package(tmp_path, "mid", "rust-mid", "1.5.0")
    needed = rd.resolver.resolve([("top", "*", "(test)", ["default"])], rd.fedora.FedoraIndex({}), rd.packages.local_packages(tmp_path))
    assert set(needed) == {"top@2.0.0"}  # mid is covered by the local package


# ─── Rust workspaces ────────────────────────────────────────────────────────

def manifest_dep(name: str, req: str = "*", path: str | None = None,
                 kind: str | None = None, optional: bool = False,
                 rename: str | None = None, source: str | None = None,
                 features: list[str] | None = None) -> dict:
    """A dependency as 'cargo metadata' shapes it."""
    return {"name": name, "req": req, "path": path, "kind": kind, "optional": optional,
            "features": features or [], "default_features": True, "uses_default_features": True,
            "target": None, "registry": None if path else "https://github.com/rust-lang/crates.io-index",
            "rename": rename, "source": source}


def workspace_fixture(root: Path, members: dict[str, list[dict]],
                      features: dict[str, dict] | None = None) -> dict:
    """A workspace on disk plus the 'cargo metadata --no-deps' output describing it."""
    (root / "Cargo.toml").write_text('[workspace]\nresolver = "2"\nmembers = ["crates/*"]\n')
    packages, ids = [], []
    for crate, deps in members.items():
        manifest = root / "crates" / crate / "Cargo.toml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(f'[package]\nname = "{crate}"\nversion = "1.0.0"\n')
        pid = f"path+file://{manifest}#{crate}"
        ids.append(pid)
        packages.append({"id": pid, "name": crate, "version": "1.0.0",
                         "manifest_path": str(manifest),
                         "features": (features or {}).get(crate, {}),
                         "dependencies": deps})
    return {"workspace_members": ids, "packages": packages}


def test_read_metadata_keeps_workspace_members_out_of_crates_io(tmp_path):
    helper = tmp_path / "crates/helper"
    meta = workspace_fixture(tmp_path, {
        "core": [manifest_dep("serde", "^1.0"),
                 manifest_dep("helper", "^1.0.0", path=str(helper)),  # a sibling with a version
                 manifest_dep("tempfile", "^3", kind="dev")],
        "helper": [],
    })
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    assert [m.crate for m in man.members] == ["core", "helper"]
    # the root manifest is the whole project
    assert [m.crate for m in man.scope] == ["core", "helper"]
    # helper is provided by the source tree; it is never asked of crates.io
    assert [s[0] for s in man.seeds] == ["serde", "tempfile"]
    assert [(r.crate, r.source) for r in man.requirements if r.crate == "helper"] == [("helper", "workspace")]


def test_read_metadata_scopes_to_the_member_that_was_asked_for(tmp_path):
    meta = workspace_fixture(tmp_path, {
        "core": [manifest_dep("serde", "^1.0")],
        "tool": [manifest_dep("clap", "^4.5")],
    })
    man = rd.resolver.read_metadata(meta, tmp_path / "crates/tool/Cargo.toml")
    assert [m.crate for m in man.members] == ["core", "tool"]  # the workspace is still known
    assert [m.crate for m in man.scope] == ["tool"]
    assert {r.required_by for r in man.requirements} == {"tool"}
    assert [s[0] for s in man.seeds] == ["clap"]


def test_read_metadata_defers_optional_dependencies_no_feature_enables(tmp_path):
    meta = workspace_fixture(tmp_path, {"helper": [manifest_dep("libc", "^0.2", optional=True)]},
                            features={"helper": {"default": [], "native": ["dep:libc"]}})
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    assert man.seeds == []  # nothing the members build by default asks for libc
    assert [(r.crate, r.enabled_by) for r in man.optional_requirements()] == [("libc", ["native"])]


def test_read_metadata_keeps_optional_dependencies_a_default_enables(tmp_path):
    meta = workspace_fixture(tmp_path, {"helper": [manifest_dep("libc", "^0.2", optional=True)]},
                            features={"helper": {"default": ["libc"]}})
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    assert [s[0] for s in man.seeds] == ["libc"]
    assert man.optional_requirements() == []


def test_read_metadata_reports_a_path_dependency_outside_the_workspace(tmp_path):
    meta = workspace_fixture(tmp_path, {"core": [manifest_dep("other", path=str(tmp_path / "extra/other"))]})
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    assert man.seeds == []
    assert [(r.crate, r.source, r.path) for r in man.requirements] == [
        ("other", "path", str(tmp_path / "extra/other"))]


def test_read_metadata_enables_renamed_dependencies_by_their_alias(tmp_path):
    # Cargo.toml says 'alias-name = { package = "real-crate" }': the features
    # name the alias, the crates.io lookup needs the real package name
    meta = workspace_fixture(tmp_path, {"helper": [manifest_dep("real-crate", "^0.2", optional=True, rename="alias-name")]},
                            features={"helper": {"default": ["dep:alias-name"], "extra": ["dep:alias-name"]}})
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    assert [s[0] for s in man.seeds] == ["real-crate"]  # the default feature enables it
    meta = workspace_fixture(tmp_path, {"helper": [manifest_dep("real-crate", "^0.2", optional=True, rename="alias-name")]},
                            features={"helper": {"default": [], "native": ["dep:alias-name"]}})
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    assert man.seeds == []
    assert [(r.crate, r.enabled_by) for r in man.optional_requirements()] == [("real-crate", ["native"])]


def test_read_metadata_collects_features_enabled_by_member_features(tmp_path):
    # 'default = ["serde/derive"]' enables 'derive' on serde: resolve must see
    # it, or a Fedora package without that feature is mistaken for a match
    meta = workspace_fixture(tmp_path, {"core": [manifest_dep("serde", "^1.0")]},
                            features={"core": {"default": ["serde/derive"], "full": ["serde/json"]}})
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    req = next(r for r in man.requirements if r.crate == "serde")
    assert "derive" in req.features
    assert "json" not in req.features  # 'full' is not enabled by the defaults


def test_workspace_reports_features_enabled_through_member_features(tmp_path, monkeypatch):
    fedora = rd.fedora.FedoraIndex({"serde": {"1.0.200": {""}}})  # ships the crate, not the feature
    meta = workspace_fixture(tmp_path, {"core": [manifest_dep("serde", "^1.0")]},
                            features={"core": {"default": ["serde/derive"]}})
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    found = rd.workspace.classify(man, fedora, {})
    assert {i.crate: i.status for i in found} == {"serde": "features"}


def test_read_metadata_keeps_git_and_alt_registry_out_of_the_crates_io_seeds(tmp_path, monkeypatch):
    stub_cratesio(monkeypatch, {"serde": ["1.0.229"]}, {"serde": []})
    meta = workspace_fixture(tmp_path, {"core": [
        manifest_dep("serde", "^1.0",
                     source="registry+https://github.com/rust-lang/crates.io-index"),
        manifest_dep("wip", "^0.1", source="git+https://github.com/others/wip?branch=main"),
        manifest_dep("custom", "^2.0", source="registry+https://registry.example.com"),
    ]})
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    assert [s[0] for s in man.seeds] == ["serde"]  # only crates.io is asked of resolve
    assert {r.crate: r.source for r in man.requirements} == {
        "serde": "registry", "wip": "git", "custom": "alt"}
    found = rd.workspace.classify(man, rd.fedora.FedoraIndex({}), {})
    assert {i.crate: i.status for i in found} == {"serde": "new", "wip": "git", "custom": "alt"}
    assert any("git+https://github.com/others/wip" in n for i in found if i.crate == "wip" for n in i.notes)
    assert any("registry.example.com" in n for i in found if i.crate == "custom" for n in i.notes)


def test_read_metadata_follows_path_dependencies_into_the_requirements(tmp_path, monkeypatch):
    stub_cratesio(monkeypatch, {"libc": ["0.2.150"]}, {"libc": []})
    extra = tmp_path / "extra" / "other"
    extra.mkdir(parents=True)
    (extra / "Cargo.toml").write_text(
        '[package]\nname = "other"\nversion = "0.1.0"\n\n'
        '[dependencies]\nlibc = "^0.2"\ninner = { path = "../inner" }\n'
        'lazy = { version = "^3", optional = true }\n\n'
        '[features]\ndefault = []\nnative = ["dep:lazy"]\n')
    inner = tmp_path / "extra" / "inner"
    inner.mkdir()
    (inner / "Cargo.toml").write_text(
        '[package]\nname = "inner"\nversion = "0.1.0"\n\n[dependencies]\nlibc = "^0.2.150"\n')
    meta = workspace_fixture(tmp_path, {"core": [manifest_dep("other", path=str(extra))]})
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    # the path crate is a leaf in 'cargo metadata --no-deps'; its own registry
    # requirements — and its nested path crate's — reach resolve
    assert {r.required_by for r in man.requirements if r.crate == "libc"} == {"other", "inner"}
    assert [(r.crate, r.source) for r in man.requirements if r.crate == "inner"] == [("inner", "path")]
    assert [(r.crate, r.enabled_by) for r in man.optional_requirements()] == [("lazy", ["native"])]
    found = rd.workspace.classify(man, rd.fedora.FedoraIndex({}), {})
    assert {i.crate: i.status for i in found} == {
        "other": "path", "inner": "path", "libc": "new", "lazy": "optional"}
    assert {i.crate: set(i.needed_by) for i in found}["libc"] == {"other", "inner"}


def test_read_metadata_reports_a_single_member_workspace_as_a_workspace(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[workspace]\nresolver = "2"\nmembers = ["crates/only"]\n')
    manifest = tmp_path / "crates" / "only" / "Cargo.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('[package]\nname = "only"\nversion = "0.1.0"\n')
    pid = f"path+file://{manifest}#only"
    meta = {"workspace_members": [pid],
            "packages": [{"id": pid, "name": "only", "version": "0.1.0",
                          "manifest_path": str(manifest), "features": {}, "dependencies": []}]}
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    assert man.workspace  # Cargo identifies a workspace even with one member
    # a plain crate with [workspace.metadata] only is not one
    (tmp_path / "Cargo.toml").unlink()  # no workspace above it either
    plain = tmp_path / "plain" / "Cargo.toml"
    plain.parent.mkdir()
    plain.write_text('[package]\nname = "plain"\nversion = "0.1.0"\n\n[workspace.metadata.foo]\nbar = 1\n')
    pid2 = f"path+file://{plain}#plain"
    meta2 = {"workspace_members": [pid2],
             "packages": [{"id": pid2, "name": "plain", "version": "0.1.0",
                           "manifest_path": str(plain), "features": {}, "dependencies": []}]}
    man2 = rd.resolver.read_metadata(meta2, plain)
    assert not man2.workspace


def test_workspace_root_finds_the_declaration(tmp_path):
    member = tmp_path / "ws/crates/core/Cargo.toml"
    member.parent.mkdir(parents=True)
    member.write_text('[package]\nname = "core"\nversion = "0.1.0"\n')
    (tmp_path / "ws/Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    assert rd.resolver.workspace_root(member) == tmp_path / "ws"
    # [workspace.metadata] in a crate's own manifest is not a workspace declaration
    plain = tmp_path / "plain/Cargo.toml"
    plain.parent.mkdir()
    plain.write_text('[package]\nname = "plain"\nversion = "0.1.0"\n\n[workspace.metadata.foo]\nbar = 1\n')
    assert rd.resolver.workspace_root(plain) == plain.parent


def test_resolve_reports_missing_optional_dependencies_of_a_manifest(tmp_path, monkeypatch, capsys):
    stub_cratesio(monkeypatch, {"libc": ["0.2.150"]}, {"libc": []})
    meta = workspace_fixture(tmp_path, {"helper": [manifest_dep("libc", "^0.2", optional=True)]},
                            features={"helper": {"default": [], "native": ["dep:libc"]}})
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    monkeypatch.setattr(rd.resolver, "read_manifest", lambda path: man)
    monkeypatch.setattr(rd.fedora, "fedora_index",
                       lambda refresh=False, chroot=None: rd.fedora.FedoraIndex({}))
    args = argparse.Namespace(crates=[], manifest=[str(tmp_path / "Cargo.toml")], root=tmp_path,
                             target=None, ignore_local=False, local_root=None, refresh=False, json=False)
    rd.resolver.cmd_resolve(args)
    out = capsys.readouterr().out
    # reported as an optional entry, naming the feature that would need it
    assert "OPTIONAL  libc 0.2.150  <- helper (feature 'native')" in out
    assert "OPTIONAL: only needed when the named feature is enabled" in out
    # an optional dependency Fedora already ships is not missing
    monkeypatch.setattr(rd.fedora, "fedora_index",
                       lambda refresh=False, chroot=None: rd.fedora.FedoraIndex({"libc": {"0.2.150": {""}}}))
    rd.resolver.cmd_resolve(args)
    out = capsys.readouterr().out
    assert "OPTIONAL" not in out
    assert "Everything is available" in out


def test_manifest_file_accepts_a_project_directory(tmp_path, capsys):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n')
    assert rd.resolver.manifest_file(tmp_path) == tmp_path / "Cargo.toml"
    with pytest.raises(SystemExit):
        rd.resolver.manifest_file(tmp_path / "nowhere")
    assert "no Cargo.toml" in capsys.readouterr().err


def test_report_init_suggestion_pins_the_audited_versions(tmp_path, monkeypatch, capsys):
    stub_cratesio(monkeypatch, {"blake3": ["1.5.0", "2.1.0"]}, {"blake3": []})
    meta = workspace_fixture(tmp_path, {"core": [manifest_dep("blake3", "^1.5")]})
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    rd.workspace.report(man, rd.fedora.FedoraIndex({}), {})
    out = capsys.readouterr().out
    # 'init' without a version resolves '*' and would pick 2.1.0, not the
    # 1.5.0 the audit identified for the workspace requirement
    assert "rust-deps init --recursive blake3@=1.5.0" in out


def test_resolve_reports_what_fedora_and_the_tree_already_provide(tmp_path, monkeypatch):
    stub_cratesio(monkeypatch, {"blake3": ["1.5.0"]}, {"blake3": []})
    make_package(tmp_path, "packed", "rust-packed", "2.0.0")
    fedora = rd.fedora.FedoraIndex({"serde": {"1.0.200": {""}}})
    needed = rd.resolver.resolve(
        [("serde", "^1.0", "(test)", ["default"]),
         ("packed", "^2.0", "(test)", ["default"]),
         ("blake3", "^1.5", "(test)", ["default"])],
        fedora, rd.packages.local_packages(tmp_path), report_provided=True)
    assert {n.crate: n.status for n in needed.values()} == {
        "serde": "fedora", "packed": "local", "blake3": "new"}
    assert needed["serde@1.0.200"].reqs == {"^1.0"}
    # without the flag, resolve only reports what is missing
    needed = rd.resolver.resolve([("serde", "^1.0", "(test)", ["default"])], fedora, {})
    assert needed == {}


def test_workspace_classifies_every_crate_the_members_ask_for(tmp_path, monkeypatch):
    stub_cratesio(monkeypatch, {"blake3": ["1.5.0"], "tempfile": ["3.10.0"]}, {"blake3": [], "tempfile": []})
    make_package(tmp_path, "packed", "rust-packed", "2.0.0")
    fedora = rd.fedora.FedoraIndex({"serde": {"1.0.200": {""}}})
    meta = workspace_fixture(tmp_path, {
        "core": [manifest_dep("serde", "^1.0"),
                 manifest_dep("packed", "^2.0"),
                 manifest_dep("blake3", "^1.5"),
                 manifest_dep("helper", "^1.0.0", path=str(tmp_path / "crates/helper")),
                 manifest_dep("tempfile", "^3", kind="dev")],
        "helper": [],
    })
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    found = rd.workspace.classify(man, fedora, rd.packages.local_packages(tmp_path))
    assert {i.crate: i.status for i in found} == {
        "serde": "fedora", "packed": "local", "blake3": "new",
        "helper": "workspace", "tempfile": "new"}
    assert {i.crate: i.version for i in found}["helper"] == "1.0.0"  # the member's own version
    assert any("tests" in n for i in found if i.crate == "tempfile" for n in i.notes)


def test_vendored_crates_and_the_source_replacement(tmp_path):
    for name, ver in [("serde", "1.0.200"), ("quux", "0.1.0")]:
        d = tmp_path / "vendor" / f"{name}-{ver}"
        d.mkdir(parents=True)
        (d / "Cargo.toml").write_text(f'[package]\nname = "{name}"\nversion = "{ver}"\n')
    (tmp_path / "vendor/not-a-crate").mkdir()
    (tmp_path / ".cargo").mkdir()
    (tmp_path / ".cargo/config.toml").write_text('[source.crates-io]\nreplace-with = "vendored-sources"\n')
    assert rd.workspace.vendored_crates(tmp_path) == [("quux", "0.1.0"), ("serde", "1.0.200")]
    assert rd.workspace.replaced_source(tmp_path) == "crates-io"  # crates-io is the replaced source
    (tmp_path / ".cargo/config.toml").write_text('[build]\njobs = 4\n')
    assert rd.workspace.replaced_source(tmp_path) is None
    assert rd.workspace.vendored_crates(tmp_path / "nothing") == []


def test_vendor_audit_drops_only_replaced_and_satisfying_crates(tmp_path, monkeypatch, capsys):
    stub_cratesio(monkeypatch, {"old": ["2.0.0"], "lazy": ["3.1.0"]}, {"old": [], "lazy": []})
    fedora = rd.fedora.FedoraIndex({"serde": {"1.0.200": {""}},  # satisfies ^1.0
                                    "old": {"1.0.0": {""}},  # does not satisfy ^2.0
                                    "feat": {"1.0.0": {""}},  # lacks the asked feature
                                    "unused": {"0.1.0": {""}}})  # nothing asks for it
    meta = workspace_fixture(tmp_path, {"core": [manifest_dep("serde", "^1.0"),
                                                 manifest_dep("old", "^2.0"),
                                                 manifest_dep("feat", "^1.0", features=["derive"])]})
    man = rd.resolver.read_metadata(meta, tmp_path / "Cargo.toml")
    for name, ver in [("serde", "1.0.200"), ("old", "2.0.0"), ("feat", "1.0.0"), ("unused", "0.1.0")]:
        d = tmp_path / "vendor" / f"{name}-{ver}"
        d.mkdir(parents=True)
        (d / "Cargo.toml").write_text(f'[package]\nname = "{name}"\nversion = "{ver}"\n')
    # without a replace-with source the build never reads vendor/: nothing is dropped
    rd.workspace.report(man, fedora, {})
    out = capsys.readouterr().out
    assert "DROP" not in out
    assert "vendor/ is not configured as a replacement source" in out
    (tmp_path / ".cargo").mkdir()
    (tmp_path / ".cargo/config.toml").write_text('[source.crates-io]\nreplace-with = "vendored-sources"\n')
    rd.workspace.report(man, fedora, {})
    out = capsys.readouterr().out
    assert "DROP      serde 1.0.200" in out  # Fedora's 1.0.200 satisfies the requirement
    assert "KEEP      old 2.0.0" in out and "does not satisfy ^2.0" in out
    assert "KEEP      feat 1.0.0" in out and "not the features the workspace asks for" in out
    assert "KEEP      unused 0.1.0" in out and "nothing in the workspace asks for it" in out
    assert "1 of the 4 vendored crates can be dropped" in out


def test_parse_workspace_output_rows():
    rows = rd.tui_results.parse_workspace_output([
        "SYSTEM    serde 1.0.229             req ^1.0                <- core",
        "OPTIONAL  libc                      req ^0.2                <- helper",
        "PATH      other                                       <- core",  # no version, no req
        "            core 1.0.0                crates/core/Cargo.toml",  # a member line
        "SYSTEM: Fedora ships this version: the spec BuildRequires crate(<name>).",  # the legend
        "DROP      serde 1.0.200             Fedora has 1.0.229",  # a vendor audit line
    ])
    assert [(r["status"], r["crate"], r["version"], r["asked"], r["needed_by"]) for r in rows] == [
        ("SYSTEM", "serde", "1.0.229", "^1.0", "core"),
        ("OPTIONAL", "libc", "", "^0.2", "helper"),
        ("PATH", "other", "", "", "core"),  # the '<-' is not a version
    ]


def test_render_workspace_output_offers_to_package_what_fedora_lacks(tmp_path):
    table, actions = rd.tui_results.render_workspace_output(
        ["NEW       blake3 1.5.0              req ^1.5                <- core",
         "SYSTEM    serde 1.0.229             req ^1.0                <- core",
         "UPDATE    clap 4.5.4                req ^4.5                <- tool"], tmp_path)
    assert table[1] == [["NEW", "blake3", "1.5.0", "^1.5", "core"],
                        ["SYSTEM", "serde", "1.0.229", "^1.0", "core"],
                        ["UPDATE", "clap", "4.5.4", "^4.5", "tool"]]
    # an UPDATE is a decision about Fedora's package, not a new package to create
    # the NEW jump pins the version the audit identified: 'init' alone resolves '*'
    assert [a[:4] for a in actions] == [("init", ["blake3@=1.5.0"], ["recursive"], {})]


def test_crate_versions_treats_404_as_no_such_crate(monkeypatch):
    def not_found(url, key, max_age):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(rd.cratesio, "_cached_json", not_found)
    rd.cratesio.crate_versions.cache_clear()
    assert rd.cratesio.crate_versions("nosuchcrate12345") == []
    rd.cratesio.crate_versions.cache_clear()


def releases(monkeypatch, *nums):
    monkeypatch.setattr(rd.cratesio, "crate_versions", lambda name: [{"num": n, "yanked": False} for n in nums])


def test_update_version(tmp_path, monkeypatch):
    make_package(tmp_path, "native-ossl", "rust-native-ossl", "0.3.0")
    make_package(tmp_path, "hashbrown", "rust-hashbrown0.15", "0.15.2")
    pkgs = rd.packages.local_packages(tmp_path)
    releases(monkeypatch, "0.16.0", "0.15.5", "0.3.1", "0.3.0", "0.2.0")
    assert rd.update.update_version(pkgs["native-ossl"], None) == ("0.16.0", None)
    assert rd.update.update_version(pkgs["native-ossl"], "0.3.1") == ("0.3.1", None)
    assert rd.update.update_version(pkgs["native-ossl"], "0.3.0")[1] == "already at 0.3.0"
    assert "older" in rd.update.update_version(pkgs["native-ossl"], "0.2.0")[1]
    assert "not on crates.io" in rd.update.update_version(pkgs["native-ossl"], "0.4.0")[1]
    # a compat package stays in its series unless told otherwise
    assert rd.update.update_version(pkgs["hashbrown"], None) == ("0.15.5", None)
    assert "--no-compat" in rd.update.update_version(pkgs["hashbrown"], "0.16.0")[1]


def test_repin_upstream_sources(tmp_path, monkeypatch):
    old, new = "379bedbd040ff060f712f54377d41cb59e094f2b", "60f49306515247ae43aac5b370aaffc6e2abc741"
    d = make_package(tmp_path, "ring-native-ossl", "rust-ring-native-ossl", "0.3.0")
    url = "https://forge.fedoraproject.org/freeipa/native-ossl/raw/commit/{}/LICENSE"
    (d / "rust2rpm.toml").write_text(f'[[package.extra-sources]]\nnumber = 10\nfile = "{url.format(old)}"\n')
    pkg = rd.packages.local_packages(tmp_path)["ring-native-ossl"]
    monkeypatch.setattr(rd.pkginit, "url_exists", lambda u: False)
    assert rd.update.repin_upstream_sources(pkg, old, new) == [url.format(new)]
    assert old not in pkg.config_file.read_text() and new in pkg.config_file.read_text()
    assert rd.update.repin_upstream_sources(pkg, old, new) == []  # nothing pinned to the old commit any more


def test_edits_drift():
    toml = {"dependencies": {"serde": {"version": "1", "optional": True}},
            "dev-dependencies": {"criterion": "0.5", "proptest": "1"}, "features": {"serde": ["dep:serde"]}}
    edits = {"drop-dev-dependencies": ["criterion", "quickcheck"], "set-dev-version": {"proptest": "1.4"}}
    suggested = {"drop-dev-dependencies": ["criterion", "proptest"], "drop-dependencies": ["serde"],
                 "drop-features": ["serde"]}
    new, stale = rd.update.edits_drift(toml, edits, suggested)
    assert new == ["drop-dependencies: serde", "drop-features: serde"]  # proptest is handled by set-dev-version
    assert stale == ["drop-dev-dependencies: quickcheck"]


def test_broken_dependents(tmp_path):
    d = make_package(tmp_path, "freeipa", "rust-freeipa", "0.1.0")
    crate = make_crate(tmp_path, {"Cargo.toml": (
        '[package]\nname = "freeipa"\n[dependencies.native-ossl]\nversion = "^0.3"\n'
        '[dev-dependencies.native-ossl-sys]\nversion = "=0.3.0"\n', 0o644)})
    crate.rename(d / "freeipa-0.1.0.crate")
    make_package(tmp_path, "native-ossl", "rust-native-ossl", "0.4.0")
    assert rd.update.broken_dependents({"native-ossl": "0.3.1", "native-ossl-sys": "0.3.1"}, tmp_path) == \
        ["freeipa 0.1.0 needs native-ossl-sys =0.3.0 (dev)"]
    assert rd.update.broken_dependents({"native-ossl": "0.4.0"}, tmp_path) == ["freeipa 0.1.0 needs native-ossl ^0.3 (normal)"]


# ─── COPR logs ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line, label", [
    ("Problem: nothing provides requested (crate(zmij/default) >= 1.0.23 with crate(zmij/default) < 2.0.0~)",
     "zmij >=1.0.23, <2.0.0 [default]"),
    (" Problem 1: nothing provides requested (crate(outref/default) >= 0.5.0 with crate(outref/default) < 0.6.0~)",
     "outref >=0.5.0, <0.6.0 [default]"),
    ("No matching package to install: 'pkgconfig(openssl) >= 3.0'", "pkgconfig(openssl) >= 3.0"),
    ("nothing provides requested (crate(claims/default) >= 0.7.1 with crate(claims/default) <= 0.8.0)",
     "claims >=0.7.1, <=0.8.0 [default]"),
])
def test_missing_requirement_parsing(line, label):
    m = rd.copr.MISSING_RE.search(line)
    assert m and rd.copr.MissingReq.parse(m.group(1) or m.group(2)).label() == label


def test_conflict_parsing():
    line = ("  - cannot install both rust-hashbrown-devel-0.16.1-1.el10_2.noarch from epel and "
            "rust-hashbrown-devel-0.17.1-1.el10.noarch from copr_base")
    [c] = rd.copr.log_conflicts([line, line])
    assert c.crate == "hashbrown"
    assert c.sides == [("0.16.1", "epel"), ("0.17.1", "copr_base")]


def local_pkg(tmp_path: Path, drop_features: list[str]) -> "rd.LocalPackage":
    d = tmp_path / "jsonschema"
    d.mkdir(exist_ok=True)
    (d / rd.config.EDITS_FILE).write_text(f"drop-features = {json.dumps(drop_features)}\n")
    return rd.packages.LocalPackage("jsonschema", d, "0.58.0")


def test_explain_warning(tmp_path):
    pkg = local_pkg(tmp_path, ["macros"])
    assert "drops feature 'macros'" in rd.coprlogs.explain_warning("unexpected `cfg` condition value: `macros`", pkg)
    assert "upstream lint" in rd.coprlogs.explain_warning("unexpected `cfg` condition value: `other`", pkg)
    assert "rust2rpm" in rd.coprlogs.explain_warning("File listed twice: /usr/share/cargo/registry/x-1.0/LICENSE", pkg)
    assert rd.coprlogs.explain_warning("unused variable: `x`", pkg) is None


def test_summarize_log(tmp_path):
    pkg = local_pkg(tmp_path, ["macros"])
    log = "\n".join([
        "\x1b[33mwarning\x1b[0m: unexpected `cfg` condition value: `macros`",
        "  --> src/lib.rs:3:17",
        "warning: `jsonschema` (lib) generated 1 warning",
        "error[E0433]: failed to resolve: use of undeclared crate `foo`",
        "test result: FAILED. 3 passed; 1 failed; 0 ignored",
        "error: Bad exit status from /var/tmp/rpm-tmp.abc (%check)",
    ])
    missing, errors, warnings = rd.coprlogs.summarize_log(log, pkg)
    assert missing == []
    assert errors == ["error[E0433]: failed to resolve: use of undeclared crate `foo`",
                      "test result: FAILED. 3 passed; 1 failed; 0 ignored",
                      "error: Bad exit status from /var/tmp/rpm-tmp.abc (%check)"]
    [(msg, count, locs, why)] = warnings  # the "generated 1 warning" summary line is not one
    assert msg == "unexpected `cfg` condition value: `macros`" and count == 1
    assert locs == ["src/lib.rs:3:17"] and "macros" in why


# ─── tmt tests (Fedora CI) ──────────────────────────────────────────────────

def tmt_info(**kw) -> "rd.TmtInfo":
    base = dict(rpm_name="rust-zmij", crate="zmij", version="1.0.23",
                devel=["rust-zmij-devel", "rust-zmij+default-devel", "rust-zmij+no-panic-devel"],
                features=["default", "no-panic"], binaries=[],
                check_commands=["%cargo_test -- --lib", "%cargo_test -- --doc"],
                test_requires=["cargo-rpm-macros", "(crate(rand/default) >= 0.9.0 with crate(rand/default) < 0.10.0~)"])
    return rd.tmt.TmtInfo(**{**base, **kw})


def fmf(text: str) -> dict:
    return yaml.safe_load(text)


def test_render_tmt_library(tmp_path):
    files = rd.tmt.render_tmt(tmt_info())
    assert files[".fmf/version"] == "1\n"
    plan = fmf(files["plans/rust-deps.fmf"])
    assert plan["discover"] == {"how": "fmf", "filter": "tag: rust-deps"}
    tests = fmf(files["tests/rust-deps/main.fmf"])
    assert set(tests) >= {"/subpackages", "/features", "/upstream-tests"} and "/executables" not in tests
    assert tests["/features"]["environment"] == {"CRATE": "zmij", "VERSION": "1.0.23", "FEATURES": "default no-panic"}
    # dnf takes the rich dependencies as they are
    assert "(crate(rand/default) >= 0.9.0 with crate(rand/default) < 0.10.0~)" in tests["/upstream-tests"]["require"]
    assert files["tests/rust-deps/check-commands"] == "%cargo_test -- --lib\n%cargo_test -- --doc\n"
    for script in ("features.sh", "upstream-tests.sh"):
        content = files[f"tests/rust-deps/{script}"]
        assert content.startswith("#!/usr/bin/bash\n")
        path = tmp_path / script
        path.write_text(content)
        subprocess.run(["bash", "-n", str(path)], check=True)  # the generated shell must parse


def test_render_tmt_without_tests_or_library():
    files = rd.tmt.render_tmt(tmt_info(check_commands=[], test_requires=[]))
    assert "/upstream-tests" not in fmf(files["tests/rust-deps/main.fmf"])
    assert "tests/rust-deps/check-commands" not in files
    app = rd.tmt.render_tmt(tmt_info(devel=[], features=[], binaries=["synta-tools"], check_commands=[]))
    assert set(fmf(app["tests/rust-deps/main.fmf"])) >= {"/executables"}
    assert "/features" not in fmf(app["tests/rust-deps/main.fmf"])


def test_write_tmt_replaces_earlier_files(tmp_path, monkeypatch):
    pkg = rd.packages.LocalPackage("zmij", tmp_path, "1.0.23")
    stale = tmp_path / "tests" / "rust-deps" / "executables.sh"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    monkeypatch.setattr(rd.tmt, "tmt_info", lambda p: tmt_info())
    rd.tmt.write_tmt(pkg)
    assert not stale.exists()
    assert (tmp_path / "tests/rust-deps/features.sh").stat().st_mode & 0o111
    assert (tmp_path / "plans/rust-deps.fmf").exists()


@pytest.mark.parametrize("chroot, image", [
    ("fedora-45-x86_64", "registry.fedoraproject.org/fedora:45"),
    ("fedora-rawhide-aarch64", "registry.fedoraproject.org/fedora:rawhide"),
])
def test_tmt_image(chroot, image):
    assert rd.tmt.tmt_image(chroot) == image


# ─── tmt inside a toolbox container ──────────────────────────────────────────

def test_in_toolbox_detects_the_marker(tmp_path, monkeypatch):
    marker = tmp_path / "toolboxenv"
    monkeypatch.setattr(rd.util, "TOOLBOX_MARKER", str(marker))
    assert not rd.util.in_toolbox()
    marker.write_text("")
    assert rd.util.in_toolbox()


def test_tmt_run_argv_provisions_a_container_by_default(tmp_path):
    pkg = rd.packages.LocalPackage("zmij", tmp_path, "1.0.23")
    argv = rd.tmt.tmt_run_argv(pkg, "u/p", "fedora-45-x86_64", how_local=False)
    assert argv == ["tmt", "--root", str(tmp_path), "run", "--all",
                    "provision", "--how", "container",
                    "--image", "registry.fedoraproject.org/fedora:45",
                    "prepare", "--insert", "--how", "install", "--order", "30",
                    "--copr", "u/p"]
    assert "--feeling-safe" not in argv  # provisioning a container needs no such opt-in


def test_tmt_run_argv_runs_locally_when_the_container_is_the_env(tmp_path):
    pkg = rd.packages.LocalPackage("zmij", tmp_path, "1.0.23")
    argv = rd.tmt.tmt_run_argv(pkg, "u/p", "fedora-45-x86_64", how_local=True)
    assert "--image" not in argv  # tmt does not provision; the throw-away container is the env
    # --how local runs on localhost, which tmt guards behind --feeling-safe (safe here: a
    # throw-away container); the opt-in is a tmt-level option, before 'run'
    assert argv[:10] == ["tmt", "--feeling-safe", "--root", str(tmp_path), "run", "--all",
                         "provision", "--how", "local", "prepare"]
    assert argv[-2:] == ["--copr", "u/p"]


def test_toolbox_escape_runs_tmt_on_the_host_in_a_throwaway_container(tmp_path):
    argv = ["tmt", "--root", str(tmp_path), "run"]
    cmd = rd.tmt.toolbox_escape(argv, tmp_path, "registry.fedoraproject.org/fedora:45")
    assert cmd[:5] == ["flatpak-spawn", "--host", "podman", "run", "--rm"]
    # label=disable so the container may read the host mounts under SELinux (relabelling the
    # host's system CA files would be wrong)
    assert "--security-opt" in cmd and "label=disable" in cmd
    assert f"{tmp_path}:{tmp_path}" in cmd            # the package dir, at its own path
    # the host's CA anchors, so the container trusts what the host trusts (the COPR repo's CA)
    assert "/etc/pki/ca-trust/source/anchors:/etc/pki/ca-trust/source/anchors:ro" in cmd
    assert "registry.fedoraproject.org/fedora:45" in cmd
    assert cmd[-3] == "bash" and cmd[-2] == "-lc"
    inner = cmd[-1]
    assert "update-ca-trust" in inner                # extract the mounted anchors first
    assert "dnf install -y tmt" in inner             # tmt installed in the throw-away container
    assert " ".join(argv) in inner                   # then the tmt command runs


def _tmt_args(tmp_path, **kw):
    base = dict(root=tmp_path, crates=["zmij"], all=False, project="u/p",
                chroot="fedora-45-x86_64", dry_run=True, host=False)
    return argparse.Namespace(**{**base, **kw})


def _staged_package(tmp_path):
    pkg_dir = make_package(tmp_path, "zmij", "rust-zmij", "1.0.23")
    (pkg_dir / "plans").mkdir()
    (pkg_dir / "plans" / "rust-deps.fmf").write_text("")  # so cmd_tmt does not (re)write the tmt files
    return pkg_dir


def test_cmd_tmt_refuses_a_toolbox_without_host(tmp_path, monkeypatch, capsys):
    _staged_package(tmp_path)
    monkeypatch.setattr(rd.util, "in_toolbox", lambda: True)
    with pytest.raises(SystemExit):
        rd.tmt.cmd_tmt(_tmt_args(tmp_path, host=False))
    assert "--host" in capsys.readouterr().err


def test_cmd_tmt_escapes_a_toolbox_with_host(tmp_path, monkeypatch):
    _staged_package(tmp_path)
    monkeypatch.setattr(rd.util, "in_toolbox", lambda: True)
    monkeypatch.setattr(rd.tmt.shutil, "which", lambda tool: "/usr/bin/" + tool)
    captured = []
    monkeypatch.setattr(rd.util, "run",
                        lambda cmd, **kw: captured.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    rd.tmt.cmd_tmt(_tmt_args(tmp_path, host=True, dry_run=False))
    assert captured and captured[0][:4] == ["flatpak-spawn", "--host", "podman", "run"]
    assert "provision --how local" in captured[0][-1]  # local provisioning inside the throw-away container


def test_cmd_tmt_runs_directly_outside_a_toolbox(tmp_path, monkeypatch):
    _staged_package(tmp_path)
    monkeypatch.setattr(rd.util, "in_toolbox", lambda: False)
    monkeypatch.setattr(rd.tmt.shutil, "which", lambda tool: "/usr/bin/" + tool)
    captured = []
    monkeypatch.setattr(rd.util, "run",
                        lambda cmd, **kw: captured.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    rd.tmt.cmd_tmt(_tmt_args(tmp_path, host=False, dry_run=False))
    assert captured[0][0] == "tmt" and "container" in captured[0]


def test_tmt_tui_form_offers_the_host_flag():
    fields = rd.tui_form.tui_fields(_subcommand_parsers()["tmt"])
    host = next(f for f in fields if f.dest == "host")
    assert host.kind == "flag" and "--host" in host.options


# ─── review plan ────────────────────────────────────────────────────────────

def test_review_plan(tmp_path, monkeypatch):
    import os
    pkgs = {}
    for crate, spec_name in [("zmij", "rust-zmij"), ("synta-derive", "rust-synta-derive"),
                             ("synta", "rust-synta"), ("synta-cbor", "rust-synta-cbor"),
                             ("ciborium", "rust-ciborium")]:
        make_package(tmp_path, crate, spec_name, "1.0.0")
    (tmp_path / "ciborium" / rd.config.TARGETS_FILE).write_text('only = ["rhel+epel-10"]\n')
    (tmp_path / "zmij" / rd.config.DIST_GIT_FILE).write_text('package = "rust-zmij"\nbranch = "rawhide"\ncommit = "2cccfd47b7"\n')
    (tmp_path / "synta-cbor" / rd.reviewrequest.REQUEST_STATE).write_text(json.dumps({"bug": 2600001}))
    draft = tmp_path / "synta-derive" / rd.reviewrequest.REQUEST_DRAFT
    draft.write_text("Summary: Review Request: rust-synta-derive - derive macros\n")
    os.utime(tmp_path / "synta-derive" / "rust-synta-derive.spec", (1_000_000_000, 1_000_000_000))
    os.utime(draft, (2_000_000_000, 2_000_000_000))  # newer than the spec; both mtimes fixed
    local = rd.packages.local_packages(tmp_path)
    stages = [[local["zmij"], local["synta-derive"], local["ciborium"]], [local["synta"]], [local["synta-cbor"]]]
    monkeypatch.setattr(rd.build, "build_stages", lambda pkgs, root, warn_outside=True: stages)
    monkeypatch.setattr(rd.build, "local_deps", lambda pkgs, root: {
        "synta": {"synta-derive", "zmij"}, "synta-cbor": {"synta", "ciborium"}})
    monkeypatch.setattr(rd.reviewplan, "dist_git_has", lambda name: name == "rust-zmij")
    monkeypatch.setattr(rd.fedora, "fedora_index", lambda refresh=False, chroot=None: rd.fedora.FedoraIndex({"zmij": {"0.9.0": {""}}}))
    monkeypatch.setattr(rd.build, "srpm_of", lambda pkg: pkg.dir / "x.src.rpm")
    monkeypatch.setattr(rd.reviewrequest, "local_review_problem", lambda pkg: None if pkg.crate == "synta-derive"
                        else "no local fedora-review result; run 'review' first")
    plan = rd.reviewplan.review_plan(list(local.values()), tmp_path, None, "fedora-rawhide-x86_64")
    by = {i.pkg.crate: i for s in plan for i in s}
    assert (by["zmij"].kind, by["ciborium"].kind) == ("update", "skip")
    assert "Rawhide: 0.9.0" in by["zmij"].detail and "adopted from dist-git rawhide 2cccfd47b7" in by["zmij"].detail
    (tmp_path / "zmij" / rd.config.DIST_GIT_FILE).write_text('package = "rust-zmij"\nbranch = "rawhide"\ncommit = "2cccfd47b7"\n'
                                                      'packaging = "local"\n')
    plan = rd.reviewplan.review_plan(list(local.values()), tmp_path, None, "fedora-rawhide-x86_64")
    marked = {i.pkg.crate: i for s in plan for i in s}
    assert marked["zmij"].kind == "skip" and "marked from dist-git" in marked["zmij"].detail
    assert marked["synta"].after == ["rust-synta-derive"]  # a marked package is not waited for
    assert (by["synta-derive"].state, by["synta-derive"].detail) == ("ready", str(draft))
    assert by["synta"].state == "not-ready" and by["synta"].after == ["rust-synta-derive", "rust-zmij"]
    assert by["synta-cbor"].state == "filed" and by["synta-cbor"].detail.endswith("/2600001")
    assert by["synta-cbor"].after == ["rust-synta"]  # ciborium is not reviewed here
    os.utime(draft, (1, 1))  # a draft older than the spec does not count
    plan = rd.reviewplan.review_plan(list(local.values()), tmp_path, None, "fedora-rawhide-x86_64")
    assert [i.state for s in plan for i in s if i.pkg.crate == "synta-derive"] == ["not-ready"]


# ─── adopted from Fedora's dist-git ─────────────────────────────────────────

def test_dist_git_files():
    tree = [{"name": n, "type": t} for n, t in [
        (".gitignore", "file"), ("0001-fix.patch", "file"), ("rust-zmij.spec", "file"), ("rust2rpm.toml", "file"),
        ("sources", "file"), ("changelog", "file"), ("zmij-fix-metadata.diff", "file"), ("plans", "dir")]]
    assert rd.distgit.dist_git_files(tree, "rust-zmij") == ["0001-fix.patch", "rust2rpm.toml", "zmij-fix-metadata.diff"]


def test_spec_similarity():
    spec = "# Generated by rust2rpm 28\nName: rust-x\nVersion: 1.0\n\n%changelog\n* one\n"
    assert rd.distgit.spec_similarity(spec, spec.replace("rust2rpm 28", "rust2rpm 27").replace("* one", "* two")) == (0, 2)
    assert rd.distgit.spec_similarity(spec, spec.replace("1.0", "1.1"))[0] == 2


def test_adopted_package_builds_where_missing(tmp_path, monkeypatch):
    d = make_package(tmp_path, "serde_assert", "rust-serde_assert", "0.8.0")
    (d / rd.config.TARGETS_FILE).write_text('only = ["fedora-45"]\n')  # ignored once adopted
    (d / rd.config.DIST_GIT_FILE).write_text('package = "rust-serde_assert"\nbranch = "rawhide"\n')
    have = {"fedora-44": {}, "fedora-rawhide": {"0.8.0": {""}}, "rhel+epel-10": {"0.7.0": {""}}}
    monkeypatch.setattr(rd.copr, "target_index", lambda chroot, refresh=False:
                        rd.fedora.FedoraIndex({"serde_assert": have[rd.fedora.chroot_release(chroot)]}))
    pkg = rd.packages.local_packages(tmp_path)["serde_assert"]
    assert pkg.limited and "in Fedora" in pkg.scope
    assert pkg.builds_in("fedora-44-x86_64") and pkg.builds_in("rhel+epel-10-aarch64")
    assert not pkg.builds_in("fedora-rawhide-x86_64")


# ─── dist-git updates ───────────────────────────────────────────────────────

def test_update_message(tmp_path):
    d = make_package(tmp_path, "zmij", "rust-zmij", "1.0.23")
    (d / "rust2rpm.toml").write_text('[package]\ncargo-toml-patch-comments = ["drop opt-level", "relax num-bigint"]\n')
    pkg = rd.packages.local_packages(tmp_path)["zmij"]
    old_config = '[package]\ncargo-toml-patch-comments = ["drop opt-level"]\n'
    msg = rd.distgit.update_message(pkg, "Version:        1.0.21\n", old_config, ["plans/rust-deps.fmf"])
    assert msg == ("Update to 1.0.23\n\n- relax num-bigint\n"
                   "- add tmt tests for Fedora CI (generated by rust-deps)\n")
    assert rd.distgit.update_message(pkg, "Version:        1.0.23\n", pkg.config_file.read_text(), []) == \
        "Update the packaging\n"


def test_update_files(tmp_path, monkeypatch):
    d = make_package(tmp_path, "vsimd", "rust-vsimd", "0.8.0")
    for rel in (".fmf/version", "plans/rust-deps.fmf", "tests/rust-deps/main.fmf"):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("x")
    monkeypatch.setattr(rd.distgit, "spec_sources", lambda spec: [
        ("Source0", "https://crates.io/api/v1/crates/vsimd/0.8.0/download#/vsimd-0.8.0.crate"),
        ("Source10", "https://raw.githubusercontent.com/Nugine/simd/abc/LICENSE"),
        ("Patch0", "vsimd-fix-metadata.diff")])
    repo, lookaside = rd.distgit.update_files(rd.packages.local_packages(tmp_path)["vsimd"])
    assert set(repo) == {"rust-vsimd.spec", "rust2rpm.toml", "vsimd-fix-metadata.diff", ".fmf/version",
                         "plans/rust-deps.fmf", "tests/rust-deps/main.fmf"}
    assert [f.name for f in lookaside] == ["vsimd-0.8.0.crate", "LICENSE"]


def test_prepare_update_refuses_when_dist_git_moved(tmp_path, monkeypatch):
    import subprocess
    def sh(*args, cwd):
        subprocess.run(args, cwd=cwd, check=True, capture_output=True)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    sh("git", "init", "-q", "-b", "rawhide", cwd=upstream)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one", cwd=upstream)
    adopted = subprocess.run(["git", "rev-parse", "HEAD"], cwd=upstream, capture_output=True, text=True).stdout.strip()
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "two", cwd=upstream)
    top = tmp_path / "dist-git"
    top.mkdir()
    sh("git", "clone", "-q", str(upstream), "rust-zmij", cwd=top)
    root = tmp_path / "root"
    root.mkdir()
    d = make_package(root, "zmij", "rust-zmij", "1.0.23")
    (d / rd.config.DIST_GIT_FILE).write_text(f'package = "rust-zmij"\nbranch = "rawhide"\ncommit = "{adopted}"\n')
    actions = []
    monkeypatch.setattr(rd.util, "action", actions.append)
    assert rd.distgit.prepare_update(rd.packages.local_packages(root)["zmij"], top, "rawhide", False) is False
    assert "moved since 'adopt'" in actions[0] and "two" in actions[0]
    assert not (d / rd.distgit.UPDATE_STATE).exists()


def test_prepare_update_uses_the_adopted_branch(tmp_path, monkeypatch):
    import subprocess
    def sh(*args, cwd):
        return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    sh("git", "init", "-q", "-b", "rawhide", cwd=upstream)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one", cwd=upstream)
    sh("git", "branch", "f45", cwd=upstream)
    adopted = sh("git", "rev-parse", "f45", cwd=upstream)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "two", cwd=upstream)
    top = tmp_path / "dist-git"
    top.mkdir()
    sh("git", "clone", "-q", str(upstream), "rust-zmij", cwd=top)
    root = tmp_path / "root"
    root.mkdir()
    d = make_package(root, "zmij", "rust-zmij", "1.0.23")
    (d / rd.config.DIST_GIT_FILE).write_text(f'package = "rust-zmij"\nbranch = "f45"\ncommit = "{adopted}"\n')
    actions = []
    monkeypatch.setattr(rd.util, "action", actions.append)
    monkeypatch.setattr(rd.distgit, "update_files", lambda pkg: ({}, []))
    pkg = rd.packages.local_packages(root)["zmij"]
    # f45 is where it was adopted, and it did not move (rawhide did)
    assert rd.distgit.prepare_update(pkg, top, None, False) is True  # no files to change: nothing to commit
    assert actions == []
    assert not (d / rd.distgit.UPDATE_STATE).exists()
    assert sh("git", "rev-parse", "HEAD", cwd=top / "rust-zmij") == adopted


# ─── CLI wiring ──────────────────────────────────────────────────────────────

def run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["rust-deps", *argv])
    rd.cli.main()


def test_cli_help_exits_zero(monkeypatch):
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "--help")
    assert e.value.code == 0


def test_cli_rejects_an_unknown_command(monkeypatch):
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "frobnicate")
    assert e.value.code == 2


def test_cli_refuses_a_missing_root(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "--root", str(tmp_path / "nope"), "status")
    err = capsys.readouterr().err
    assert "is not a directory" in err and "Traceback" not in err
    assert e.value.code == 1


def test_cli_order_json(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, "a", "rust-a")
    make_package(tmp_path, "b", "rust-b")
    stub_buildrequires(monkeypatch, {"b": ["crate(a) >= 0.1"]})
    run_cli(monkeypatch, "--root", str(tmp_path), "order", "--all", "--json")
    assert json.loads(capsys.readouterr().out) == [["a"], ["b"]]


def test_cli_reports_network_failure(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, "a", "rust-a")
    monkeypatch.setattr(rd.fedora, "fedora_index", lambda refresh=False, chroot=None: rd.fedora.FedoraIndex({}))
    def boom(name):
        raise urllib.error.URLError("no route to host")
    monkeypatch.setattr(rd.cratesio, "crate_versions", boom)
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "--root", str(tmp_path), "status")
    err = capsys.readouterr().err
    assert "network request failed" in err and "Traceback" not in err
    assert e.value.code == 1


# ─── module references ───────────────────────────────────────────────────────

def _module_api(path: Path) -> set[str]:
    """Names a module of the package defines at its top level."""
    api = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign):
            api.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            api.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            api.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            api.update((a.asname or a.name).split(".")[0] for a in node.names)
    return api


def _locally_bound(fn) -> dict[str, int]:
    """Names a function binds itself (parameters, assignments, imports), first line each."""
    bound: dict[str, int] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.arg):
            bound.setdefault(node.arg, fn.lineno)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.setdefault(node.id, node.lineno)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            bound.setdefault(node.target.id, node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.setdefault((a.asname or a.name).split(".")[0], node.lineno)
    return bound


def test_no_local_shadows_a_sibling_module_it_then_calls():
    """Components refer to each other as module.attr; a local of the same name makes
    that call an UnboundLocalError (or hits the local object instead) — 'rust-deps
    resolve' crashed in CI on 'fedora = fedora.fedora_index(...)'.
    """
    pkg = Path(rd.__file__).parent
    apis = {p.stem: _module_api(p) for p in sorted(pkg.glob("*.py")) if p.stem not in ("__init__", "__main__")}
    offenders = []
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text())
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    name = (a.asname or a.name).split(".")[0]
                    if name in apis:
                        imported[name] = apis[name]
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for name, bound_at in _locally_bound(fn).items():
                if name not in imported:
                    continue
                for use in ast.walk(fn):
                    if (isinstance(use, ast.Attribute) and isinstance(use.value, ast.Name)
                            and use.value.id == name and use.attr in imported[name]):
                        offenders.append(f"{path.name}:{use.lineno} {fn.name}() binds '{name}' at {bound_at}, "
                                         f"then calls {name}.{use.attr}")
    assert offenders == []


# ─── tui ─────────────────────────────────────────────────────────────────────

def _subcommand_parsers():
    parser = rd.cli.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices


def test_tui_forms_cover_every_subcommand():
    parsers = _subcommand_parsers()
    assert "tui" in parsers  # the UI is a command of the CLI it presents
    for name, sp in parsers.items():
        if name == "tui":
            continue
        fields = rd.tui_form.tui_fields(sp)
        actions = [a for a in sp._actions
                   if a.dest != "help" and not isinstance(a, argparse._SubParsersAction)]
        assert [f.dest for f in fields] == [a.dest for a in actions], name
        assert len({f.key for f in fields}) == len(fields), name  # widget ids stay unique
        assert all(f.kind in ("text", "multi", "number", "flag") for f in fields), name


def test_tui_fields_disambiguate_shared_dests():
    # 'regen' has --compat and --no-compat sharing dest 'compat' (mutually exclusive)
    fields = rd.tui_form.tui_fields(_subcommand_parsers()["regen"])
    compat = [f for f in fields if f.dest == "compat"]
    assert [f.key for f in compat] == ["compat", "compat-2"]
    assert [f.options[0] for f in compat] == ["--compat", "--no-compat"]
    argv = rd.tui_form.tui_argv(fields, {f.key: f.key == "compat-2" for f in fields if f.kind == "flag"}
                       | {f.key: "" for f in fields if f.kind != "flag"})
    assert argv == ["--no-compat"]


def test_tui_controls_cover_every_field_exactly_once():
    for name, sp in _subcommand_parsers().items():
        if name == "tui":
            continue
        fields = rd.tui_form.tui_fields(sp)
        covered = [f for c in rd.tui_form.tui_controls(sp) for f in c.fields]
        assert len(covered) == len(fields), name
        assert sorted(f.key for f in covered) == sorted(f.key for f in fields), name


def test_tui_controls_shape_parser_structure():
    controls = rd.tui_form.tui_controls(_subcommand_parsers()["regen"])
    shape = [(c.kind, [f.key for f in c.fields]) for c in controls]
    # with_sel positional + --all become one picker; each exclusive group one choice control
    assert shape == [
        ("crates", ["crates", "all"]),
        ("choice", ["latest", "version", "crate_file"]),
        ("choice", ["compat", "compat-2"]),
    ]
    # repeatable options become multi controls; a flag-only command has no controls beyond fields
    assert [c.kind for c in rd.tui_form.tui_controls(_subcommand_parsers()["check-targets"])
            if c.kind == "multi"] == ["multi"]
    assert [c.kind for c in rd.tui_form.tui_controls(_subcommand_parsers()["doctor"])] == []
    # resolve/init have the positional but no --all: the picker covers the positional alone
    assert [c.fields[0].dest for c in rd.tui_form.tui_controls(_subcommand_parsers()["resolve"])
            if c.kind == "crates"] == ["crates"]


def test_tui_missing_required_gates_the_run():
    cf = rd.tui_form.tui_fields(_subcommand_parsers()["copr"])
    empty = {f.key: "" for f in cf} | {f.key: False for f in cf if f.kind == "flag"}
    assert rd.tui_form.tui_missing_required(cf, empty) == ["--project"]
    assert rd.tui_form.tui_missing_required(cf, empty | {"project": "u/p"}) == []
    inf = rd.tui_form.tui_fields(_subcommand_parsers()["init"])
    assert rd.tui_form.tui_missing_required(inf, {"crates": "  "}) == ["CRATE[@REQ]"]
    assert rd.tui_form.tui_missing_required(inf, {"crates": "serde"}) == []


def test_parse_trial_output_reads_trial_blocks():
    lines = [
        "== trial synta 0.4.0 (log: /home/u/.cache/rust-packaging-tools/trial/synta.log)",
        "   all                      FAILED  0 passed, 0 failed",
        "      error[E0432]: unresolved import `synta_certificate`",
        "      error: could not compile `synta` (example \"asn1parse\") due to 1 previous error",
        "== trial synta-mtc 0.4.0 (log: /home/u/.cache/rust-packaging-tools/trial/synta-mtc.log)",
        "   all                      ok      530 passed, 0 failed",
        "   tests/ui                 skipped needs feature(s) ui",
        "   tests/broken            BROKEN  cargo test failed",
        "   all test targets pass; no [tests] restrictions needed",
    ]
    rows = rd.tui_results.parse_trial_output(lines)
    assert [(r["crate"], r["target"], r["status"]) for r in rows] == [
        ("synta", "all", "FAILED"), ("synta-mtc", "all", "ok"),
        ("synta-mtc", "tests/ui", "skipped"), ("synta-mtc", "tests/broken", "BROKEN")]
    assert rows[0]["detail"].startswith("error[E0432]")
    assert rows[1]["detail"] == ""
    assert rows[0]["log"].endswith("synta.log")


def test_trial_records_an_all_passing_discovery(tmp_path):
    make_package(tmp_path, "a", "rust-a", "0.1.0")
    cfg = tmp_path / "a" / "rust2rpm.toml"
    cfg.write_text('[package]\nsummary = "A"\n\n[tests]\nrun = ["doc"]\ncomments = ["old"]\n')
    pkg = rd.packages.local_packages(tmp_path)["a"]
    rd.trial.write_tests_table(pkg, rd.trial.ALL_PASS_TABLE)
    # rust2rpm's default, said out loud: no comments to explain, the rest kept
    assert cfg.read_text() == '[package]\nsummary = "A"\n\n[tests]\nrun = true\ncomments = []\n'
    # rust2rpm's schema requires 'comments' in a [tests] table
    assert tomllib.loads(cfg.read_text())["tests"] == {"run": True, "comments": []}
    # the package leaves "Choose tests" and nothing is left to explain
    assert rd.tui_overview.tui_stage(pkg).name == "srpm?"


def test_tui_trial_actions_record_an_all_passing_discovery():
    found = rd.tui_results.parse_trial_output([
        "== trial a 1.0 (log: /l/a.log)", "   lib                      ok      2 passed, 0 failed",
        "   all test targets pass; no [tests] restrictions needed",
    ])
    assert [(c, cr, fl) for c, cr, fl, _, _ in rd.tui_results.tui_trial_actions(found)] == [
        ("trial", ["a"], ["discover", "apply"])]
    recorded = rd.tui_results.parse_trial_output([
        "== trial a 1.0 (log: /l/a.log)", "   lib                      ok      2 passed, 0 failed",
        "   all test targets pass; no [tests] restrictions needed",
        "Written to /r/a/rust2rpm.toml: every test target runs (run = true).",
    ])
    assert [(c, cr, fl) for c, cr, fl, _, _ in rd.tui_results.tui_trial_actions(recorded)] == [
        ("srpm", ["a"], [])]


def test_tui_trial_actions_turns_verdicts_into_next_steps():
    rows = rd.tui_results.parse_trial_output([
        "== trial a 1.0 (log: /l/a.log)", "   all                      ok      2 passed, 0 failed",
        "== trial b 1.0 (log: /l/b.log)", "   all                      FAILED  0 passed, 0 failed",
        "      error: could not compile",
    ])
    actions = rd.tui_results.tui_trial_actions(rows)
    assert ("trial", ["b"], ["discover"], {}, actions[0][4]) == actions[0]
    assert "discover" in actions[0][2]
    assert ("srpm", ["a"], [], {}, actions[1][4]) == actions[1]


def test_parse_trial_output_marks_discovery_and_recheck_blocks():
    rows = rd.tui_results.parse_trial_output([
        "== trial a 1.0 (log: /l/a.log)",
        "   test:x                     FAILED  0 passed, 1 failed",
        "Suggested [tests] table for rust2rpm.toml (replace the TODO comments with real reasons):",
        "[tests]",
        "run = []",
        "   Written to /r/a/rust2rpm.toml; regenerating the spec and re-running the trial.",
        "== trial a 1.0 (log: /l/a.recheck.log)",
        "   all                      ok      2 passed, 0 failed",
    ])
    assert [(r["crate"], r["mode"], r["applied"]) for r in rows] == [
        ("a", "discover", True), ("a", "recheck", False)]


def test_tui_trial_actions_chain_discovery_to_apply_and_regen():
    discovered = rd.tui_results.parse_trial_output([
        "== trial a 1.0 (log: /l/a.log)",
        "   test:x                     FAILED  0 passed, 1 failed",
        "Suggested [tests] table for rust2rpm.toml:",
    ])
    actions = rd.tui_results.tui_trial_actions(discovered)
    assert actions[0][:3] == ("trial", ["a"], ["discover", "apply"])
    assert "rechecks" in actions[0][4]
    applied_ok = rd.tui_results.parse_trial_output([
        "== trial a 1.0 (log: /l/a.log)",
        "   test:x                     FAILED  0 passed, 1 failed",
        "Suggested [tests] table for rust2rpm.toml:",
        "   Written to /r/a/rust2rpm.toml; regenerating the spec and re-running the trial.",
        "== trial a 1.0 (log: /l/a.recheck.log)",
        "   all                      ok      2 passed, 0 failed",
    ])
    actions = rd.tui_results.tui_trial_actions(applied_ok)
    # the applied table always carries TODO comments: the in-UI editor is the next step
    assert actions[0][:3] == ("tests-edit", ["a"], [])
    assert "reasons" in actions[0][4]
    applied_bad = rd.tui_results.parse_trial_output([
        "== trial a 1.0 (log: /l/a.log)",
        "   test:x                     FAILED  0 passed, 1 failed",
        "Suggested [tests] table for rust2rpm.toml:",
        "   Written to /r/a/rust2rpm.toml; regenerating the spec and re-running the trial.",
        "== trial a 1.0 (log: /l/a.recheck.log)",
        "   all                      FAILED  0 passed, 1 failed",
    ])
    actions = rd.tui_results.tui_trial_actions(applied_bad)
    assert actions[0][:3] == ("trial", ["a"], ["discover"])
    assert "still fails" in actions[0][4]


def test_parse_srpm_output_reads_srpm_blocks():
    rows = rd.tui_results.parse_srpm_output([
        "== a 0.1.0",
        "   Wrote: /root/a/a-0.1.0-1.fc45.src.rpm",
        "   %prep ok",
        '   1 packages and 0 specfiles checked; 0 errors, 0 warnings, 0 filtered, 0 aborted, rating: "OK"',
        "== b 0.2.0",  # %prep failed: the error text went to the log, the block just ends
        "   Wrote: /root/b/b-0.2.0-1.fc45.src.rpm",
        "== c 0.3.0",  # failed before writing an SRPM
        "== d 1.0",  # --no-prep: the rpmlint line proves the run continued past %prep
        "   Wrote: /root/d/d-1.0-1.fc45.src.rpm",
        '   1 packages and 0 specfiles checked; 2 errors, 0 warnings, 0 filtered, 0 aborted, rating: "FATALLY FAILED"',
    ])
    assert [(r["crate"], r["srpm"], r["prep"], r["rpmlint"]) for r in rows] == [
        ("a", "a-0.1.0-1.fc45.src.rpm", "ok",
         '0 errors, 0 warnings, 0 filtered, 0 aborted, rating: "OK"'),
        ("b", "b-0.2.0-1.fc45.src.rpm", "FAILED", "FAILED"),
        ("c", "FAILED", "FAILED", "FAILED"),
        ("d", "d-1.0-1.fc45.src.rpm", "skipped",
         '2 errors, 0 warnings, 0 filtered, 0 aborted, rating: "FATALLY FAILED"'),
    ]
    assert rd.tui_results.parse_srpm_output(["no blocks here"]) == []


def test_tui_stage_follows_the_lifecycle_graph(tmp_path):
    make_package(tmp_path, "a", "rust-a", "0.1.0")
    pkg = rd.packages.local_packages(tmp_path)["a"]
    assert rd.tui_overview.tui_stage(pkg).name == "tests?"  # spec exists, no [tests] yet
    (tmp_path / "a" / "rust2rpm.toml").write_text("[tests]\n")
    assert rd.tui_overview.tui_stage(pkg).name == "srpm?"
    (tmp_path / "a" / "rust-a-0.1.0-1.fc45.src.rpm").write_text("")
    assert rd.tui_overview.tui_stage(pkg).name == "review"
    (tmp_path / "a" / rd.reviewrequest.REQUEST_DRAFT).write_text("Summary: x")
    assert rd.tui_overview.tui_stage(pkg).name == "draft"
    (tmp_path / "a" / rd.reviewrequest.REQUEST_STATE).write_text('{"bug": 1}\n')
    assert rd.tui_overview.tui_stage(pkg).name == "filed"
    # the adopted branch leaves the review line; the build stages still come first
    (tmp_path / "a" / rd.config.DIST_GIT_FILE).write_text('[dist-git]\npackage = "rust-a"\n')
    assert rd.tui_overview.tui_stage(pkg).name == "adopted"
    (tmp_path / "a" / rd.reviewrequest.REQUEST_STATE).unlink()
    (tmp_path / "a" / "rust-a-0.1.0-1.fc45.src.rpm").unlink()
    assert rd.tui_overview.tui_stage(pkg).name == "srpm?"  # the build stages still come first
    (tmp_path / "a" / rd.config.DIST_GIT_FILE).unlink()
    (tmp_path / "a" / "rust-a-0.1.0-1.fc45.src.rpm").write_text("")
    (tmp_path / "a" / rd.config.TARGETS_FILE).write_text('only = ["fedora-44"]\n')
    assert rd.tui_overview.tui_stage(pkg).name == "targets"


def test_tui_argv_from_form_values():
    fields = rd.tui_form.tui_fields(_subcommand_parsers()["status"])
    argv = rd.tui_form.tui_argv(fields, {"crates": "serde jsonschema", "all": False, "target": "fedora-44",
                                "project": "", "all_versions": True, "refresh": False})
    assert argv == ["serde", "jsonschema", "--chroot", "fedora-44", "--all-versions"]


def test_tui_argv_repeats_append_options_and_skips_empty_fields():
    fields = rd.tui_form.tui_fields(_subcommand_parsers()["resolve"])
    argv = rd.tui_form.tui_argv(fields, {"crates": "jsonschema@^1", "manifest": "/a/Cargo.toml /b/Cargo.toml",
                                "ignore_local": True, "local_root": "", "json": False,
                                "target": "fedora-44-x86_64", "refresh": False})
    assert argv == ["jsonschema@^1", "--manifest", "/a/Cargo.toml", "--manifest", "/b/Cargo.toml",
                    "--ignore-local", "--chroot", "fedora-44-x86_64"]


def test_tui_argv_expands_a_tilde_the_way_a_shell_would(tmp_path, monkeypatch):
    """A form runs its command as a subprocess: nothing else expands a '~' typed here."""
    monkeypatch.setenv("HOME", str(tmp_path))
    fields = rd.tui_form.tui_fields(_subcommand_parsers()["workspace"])
    argv = rd.tui_form.tui_argv(
        fields, {"projects": "~/ws ~/other/Cargo.toml", "local_root": "~"}
    )
    assert argv == [
        str(tmp_path / "ws"),
        str(tmp_path / "other" / "Cargo.toml"),
        "--local-root",
        str(tmp_path),
    ]
    # a '~' that is not the start of a path stays exactly as typed
    fields = rd.tui_form.tui_fields(_subcommand_parsers()["init"])
    assert rd.tui_form.tui_argv(fields, {"crates": "serde@~1.0 zmij"}) == [
        "serde@~1.0",
        "zmij",
    ]
    assert rd.tui_form.expand_user_path("~1.0") == "~1.0"
    assert rd.tui_form.expand_user_path("~nosuchuser123/x") == "~nosuchuser123/x"


def test_tui_argv_keeps_each_repeatable_row_as_one_argument():
    # a row's value is a list entry, not a token in a joined string: a project
    # path with a space reaches 'workspace' as the one argument it was typed as
    fields = rd.tui_form.tui_fields(_subcommand_parsers()["workspace"])
    argv = rd.tui_form.tui_argv(fields, {"projects": ["/tmp/my workspace", "/other/Cargo.toml"],
                                         "local_root": []})
    assert argv == ["/tmp/my workspace", "/other/Cargo.toml"]
    assert rd.tui_form.tui_missing_required(fields, {"projects": []})
    assert not rd.tui_form.tui_missing_required(fields, {"projects": ["/tmp/my workspace"]})


def test_tui_fields_prefill_defaults_and_type_numbers():
    fields = {f.dest: f for f in rd.tui_form.tui_fields(_subcommand_parsers()["mock-chain"])}
    assert fields["chroot"].default == "fedora-rawhide-x86_64"
    fields = {f.dest: f for f in rd.tui_form.tui_fields(_subcommand_parsers()["trial"])}
    assert fields["timeout"].kind == "number" and fields["timeout"].default == "900"


def test_parse_columns_table_reads_status_output():
    lines = ["crate  packaged  crates.io  fedora-45  patch  srpm",
             "serde  1.0.217  1.0.220 *  -  yes  -",
             "jsonschema (compat)  0.58.0  0.58.0  0.58.0=  -  yes",
             "",
             "* = newer version on crates.io ('rust-deps update <crate>' to update)"]
    assert rd.tui_results.parse_columns_table(lines) == (
        ["crate", "packaged", "crates.io", "fedora-45", "patch", "srpm"],
        [["serde", "1.0.217", "1.0.220 *", "-", "yes", "-"],
         ["jsonschema (compat)", "0.58.0", "0.58.0", "0.58.0=", "-", "yes"]])


def _tui_commands():
    return {name: ("", rd.tui_form.tui_fields(p), rd.tui_form.tui_controls(p))
            for name, p in _subcommand_parsers().items()}


def test_tui_suggested_actions_follows_what_the_tools_propose(tmp_path):
    make_package(tmp_path, "synta", "rust-synta", "0.4.0")
    make_package(tmp_path, "synta-derive", "rust-synta-derive", "0.4.0")
    commands = _tui_commands()
    plan = [
        "Submit in this order: a ticket names the tickets of the packages it waits for.",
        "",
        "=== stage 1",
        "  1. rust-synta 0.4.0  READY",
        "       /r/synta/review-request.txt",
        "  2. rust-synta-derive 0.4.0  NOT READY",
        "       no local fedora-review result; run 'review' first",
        "       after: synta",
        "",
        "summary: 1 ready, 1 not-ready",
        "file the READY ones, in this order: rust-deps review-request --project u/p --fas NAME --file synta",
    ]
    acts = rd.tui_overview.tui_suggested_actions(plan, tmp_path, commands)
    triples = [(c, cr, fl) for c, cr, fl, _, _ in acts]
    # review-plan's own wording names the step and the package block picks the crate
    assert ("review", ["synta-derive"], []) in triples
    assert next(why for c, _, _, _, why in acts if c == "review") == \
        "no local fedora-review result; run 'review' first"
    # a full rust-deps line carries its own crates and flags; option values are skipped
    assert ("review-request", ["synta"], ["file"]) in triples

    status = [
        "== rust-synta: https://bugzilla.redhat.com/show_bug.cgi?id=2361903",
        "   NEW; fedora-review?; reviewer nobody; last change 2026-10-01",
        '   next: address the comments above: fix, regen, srpm, review, copr --wait, '
        'then \'review-request --file --comment "<what changed>"\'',
    ]
    triples = [(c, cr, fl) for c, cr, fl, _, _ in rd.tui_overview.tui_suggested_actions(status, tmp_path, commands)]
    assert ("review-request", ["synta"], ["file"]) in triples

    copr = [
        "=== stage 1",
        "synta-derive 0.4.0-1.fc46: build 4488123",
        "   RETRY    fedora-rawhide-x86_64",
        "            rust-bar-devel: nothing provides it",
        "",
        "summary: 1 retry",
        "- resubmit what can succeed now:",
        "    rust-deps --root /r copr --retry-failed --project u/p synta-derive",
    ]
    triples = [(c, cr, fl) for c, cr, fl, _, _ in rd.tui_overview.tui_suggested_actions(copr, tmp_path, commands)]
    assert ("copr", ["synta-derive"], ["retry_failed"]) in triples

    # prose that quotes non-commands suggests nothing
    noise = [
        "== trial synta 0.4.0 (log: /l/synta.log)",
        "   all                      FAILED  0 passed, 1 failed",
        "      don't use 'cargo' directly; the mock's 'cargo test' macro runs it",
    ]
    assert rd.tui_overview.tui_suggested_actions(noise, tmp_path, commands) == []
    # a real proposal with a placeholder crate still jumps, just without crates
    footer = ["* = newer version on crates.io ('rust-deps update <crate>' to update)"]
    assert rd.tui_overview.tui_suggested_actions(footer, tmp_path, commands) == \
        [("update", [], [], {}, footer[0])]


def test_tui_suggested_actions_captures_literal_option_values(tmp_path):
    make_package(tmp_path, "synta-derive", "rust-synta-derive", "0.4.0")
    commands = _tui_commands()
    lines = [
        "=== stage 1",
        "synta-derive 0.4.0-1.fc46: build 4488123",
        "   RETRY    fedora-rawhide-x86_64",
        "",
        "summary: 1 retry",
        "- resubmit what can succeed now:",
        "    rust-deps --root /r copr --retry-failed --project u/p -r fedora-44-x86_64 synta-derive",
    ]
    acts = rd.tui_overview.tui_suggested_actions(lines, tmp_path, commands)
    copr = next(a for a in acts if a[0] == "copr")
    assert copr[1] == ["synta-derive"] and "retry_failed" in copr[2]
    assert copr[3] == {"project": "u/p", "chroot": "fedora-44-x86_64"}
    # metavar placeholders (P, NAME) are not values; repeatable options accumulate
    plan = ["file the READY ones: rust-deps review-request --project P --fas NAME --file synta-derive"]
    rr = rd.tui_overview.tui_suggested_actions(plan, tmp_path, commands)[0]
    assert rr[:4] == ("review-request", ["synta-derive"], ["file"], {})
    two = ["    rust-deps copr --project u/p -r fedora-44-x86_64 -r epel-10-x86_64 synta-derive"]
    assert rd.tui_overview.tui_suggested_actions(two, tmp_path, commands)[0][3] == {
        "project": "u/p", "chroot": "fedora-44-x86_64 epel-10-x86_64"}


def test_tui_suggested_actions_maps_a_single_positional_crate(tmp_path):
    make_package(tmp_path, "synta", "rust-synta", "0.4.0")
    commands = _tui_commands()
    lines = [
        "=== stage 1",
        "synta 0.4.0-1.fc46: build 1",
        "   FAILED   fedora-44-x86_64",
        "- build errors: read the summary:",
        "    rust-deps --root /r copr-log synta -r fedora-44-x86_64 --project u/p",
    ]
    acts = rd.tui_overview.tui_suggested_actions(lines, tmp_path, commands)
    log = next(a for a in acts if a[0] == "copr-log")
    # 'copr-log' takes one crate as a positional: it arrives as a form value, not a picker
    assert log[1] == []
    assert log[3] == {"chroot": "fedora-44-x86_64", "project": "u/p", "crate": "synta"}


def test_tui_playbooks_cover_every_command():
    parsers = _subcommand_parsers()
    assert set(rd.tui_playbooks.TUI_PLAYBOOKS) == set(parsers) - {"tui"}
    for name, pb in rd.tui_playbooks.TUI_PLAYBOOKS.items():
        if pb.result:
            assert pb.result in rd.tui_playbooks.TUI_RESULT_RENDERERS, name
        # only tools 'doctor' actually reports can gate the sidebar
        for tool in pb.requires:
            assert tool in rd.doctor.OPTIONAL_TOOLS, name


def test_parse_copr_status_output_reads_the_ladder():
    rows = rd.tui_results.parse_copr_status_output([
        "=== stage 1",
        "synta-derive 0.4.0-1.fc46: build 4488123",
        "   RETRY    fedora-rawhide-x86_64",
        "            rust-bar-devel: nothing provides it",
        "   ok       fedora-44-x86_64  fedora-45-x86_64  (build https://copr.fedorainfracloud.org/coprs/build/4488000/)",
        "native-ossl 0.1.0-1.fc46: no COPR build of this version",
        "=== stage 2",
        "synta 0.4.0-1.fc46: build 4488200",
        "   BLOCKED  fedora-rawhide-x86_64",
        "",
        "summary: 1 ok, 1 retry, 1 blocked",
        "- resubmit what can succeed now:",
        "    rust-deps --root /r copr --retry-failed --project u/p synta-derive",
    ])
    assert [(r["stage"], r["crate"], r["verdict"], r["chroots"]) for r in rows] == [
        (1, "synta-derive", "RETRY", "fedora-rawhide-x86_64"),
        (1, "synta-derive", "ok", "fedora-44-x86_64 fedora-45-x86_64"),
        (1, "native-ossl", "none", ""),
        (2, "synta", "BLOCKED", "fedora-rawhide-x86_64"),
    ]
    assert rd.tui_results.parse_copr_status_output(["nothing here"]) == []


def test_tests_table_comments_and_replace(tmp_path):
    text = ('[package]\nsummary = "x"\n\n[tests]\nrun = ["lib"]\n'
            'skip."test:x" = ["a"]\nskip-exact."test:x" = true\n'
            'comments = [\n    "TODO: explain why test:x is disabled (fails at runtime)",\n]\n'
            '\n[features]\nui = []\n')
    assert rd.tui_overview.tests_table_comments(text) == [
        "TODO: explain why test:x is disabled (fails at runtime)"]
    new = rd.tui_overview.replace_tests_comments(text, ["test:x needs a network; %cargo_test skips it"])
    # only the comments array changes; the rest of the file and table is untouched
    assert new.startswith('[package]\nsummary = "x"\n\n[tests]\nrun = ["lib"]\n')
    assert 'comments = [\n    "test:x needs a network; %cargo_test skips it",\n]' in new
    assert "[features]" in new and "TODO" not in new
    # a table without a comments array is left alone
    plain = '[tests]\nrun = false\n'
    assert rd.tui_overview.tests_table_comments(plain) == []
    assert rd.tui_overview.replace_tests_comments(plain, ["x"]) == plain


def test_parse_review_plan_output_reads_the_board():
    rows = rd.tui_results.parse_review_plan_output([
        "Submit in this order: a ticket names the tickets of the packages it waits for.",
        "",
        "=== stage 1",
        "  1. rust-zmij 1.0.0  update",
        "       adopted from dist-git rawhide 2cccfd47b7",
        "  2. rust-synta-derive 0.4.0  READY",
        "       /r/synta-derive/review-request.txt",
        "=== stage 2",
        "  3. rust-synta 0.4.0  NOT READY",
        "       no local fedora-review result; run 'review' first",
        "       after: rust-synta-derive",
        "",
        "no review needed:",
        "     rust-ciborium 1.0.0: limited to rhel+epel-10",
        "summary: 1 ready, 1 not-ready",
    ])
    assert [(r["stage"], r["package"], r["state"], r["after"]) for r in rows] == [
        (1, "rust-zmij", "update", ""),
        (1, "rust-synta-derive", "READY", ""),
        (2, "rust-synta", "NOT READY", "rust-synta-derive"),
    ]
    assert rows[1]["detail"] == "/r/synta-derive/review-request.txt"
    assert rd.tui_results.parse_review_plan_output(["nothing here"]) == []


def test_review_request_results_gate_the_filing(tmp_path):
    make_package(tmp_path, "synta", "rust-synta", "0.4.0")
    (tmp_path / "synta" / rd.reviewrequest.REQUEST_DRAFT).write_text(
        "Summary: Review Request: rust-synta - Merkle tree certs\n\n"
        "Spec URL: https://copr.example/synta.spec\n"
        "SRPM URL: https://copr.example/synta-0.4.0-1.fc46.src.rpm\n"
        "Upstream URL: https://crates.io/crates/synta\n")
    parsed, actions = rd.tui_results.render_review_request_output(
        ["== review request rust-synta 0.4.0", "   draft: /r/synta/review-request.txt"], tmp_path)
    assert parsed[0] == ["crate", "version", "summary", "spec", "srpm"]
    assert parsed[1][0][:3] == ["synta", "0.4.0", "Review Request: rust-synta - Merkle tree certs"]
    # the gate: filing is offered only while the draft has no ticket
    assert actions[0][:3] == ("review-request", ["synta"], ["file"])
    (tmp_path / "synta" / rd.reviewrequest.REQUEST_STATE).write_text('{"bug": 1}')
    parsed, actions = rd.tui_results.render_review_request_output(
        ["== review request rust-synta 0.4.0"], tmp_path)
    assert parsed is not None and actions == []


def test_resolve_results_carry_to_init(tmp_path):
    lines = [
        "NEW       jsonschema 0.21.0  <- synta",
        "UPDATE    serde 1.0.220 (Fedora has 1.0.217)  <- synta",
        "FEATURES  openssl 0.10.7  <- synta-mtc",
        "            missing features in Fedora package: v0",
    ]
    rows = rd.tui_results.parse_resolve_output(lines)
    assert [(r["status"], r["crate"], r["version"]) for r in rows] == [
        ("NEW", "jsonschema", "0.21.0"), ("UPDATE", "serde", "1.0.220"),
        ("FEATURES", "openssl", "0.10.7")]
    parsed, actions = rd.tui_results.render_resolve_output(lines, tmp_path)
    # only NEW crates become packages to create; UPDATE/FEATURES are decisions, not jumps
    # the jump pins the version this run identified: 'init' alone resolves '*'
    assert actions[0][:3] == ("init", ["jsonschema@=0.21.0"], ["recursive"])


def test_update_dry_run_becomes_an_apply_jump():
    dry = rd.tui_results.parse_update_output([
        "== synta-derive: no spec yet; 'rust-deps regen synta-derive' generates one",
        "Updates, in build order: zmij 0.9.0 -> 1.0.0, synta 0.4.0 -> 0.5.0",
    ])
    assert [(r["crate"], r["to"], r["note"]) for r in dry] == [
        ("synta-derive", "-", "no spec yet; 'rust-deps regen synta-derive' generates one"),
        ("zmij", "1.0.0", ""), ("synta", "0.5.0", "")]
    parsed, actions = rd.tui_results.render_update_output(
        ["Updates, in build order: zmij 0.9.0 -> 1.0.0"], Path("/unused"))
    assert actions[0][:2] == ("update", ["zmij"])
    # a completed update shows what it did, and does not offer to redo it
    done = rd.tui_results.parse_update_output([
        "Updates, in build order: zmij 0.9.0 -> 1.0.0",
        "== zmij 0.9.0 -> 1.0.0",
    ])
    assert [(r["crate"], r["note"]) for r in done] == [("zmij", "updated")]
    assert rd.tui_results.render_update_output(["== zmij 0.9.0 -> 1.0.0"], Path("/unused"))[1] == []


def test_render_order_output_shows_the_ladder():
    parsed, actions = rd.tui_results.render_order_output(
        ["stage 1: zmij synta-derive", "stage 2: synta"], Path("/unused"))
    assert parsed == (["stage", "packages"],
                      [["1", "zmij synta-derive"], ["2", "synta"]])
    assert actions == []
    assert rd.tui_results.render_order_output(["no stages here"], Path("/unused")) == (None, [])


def test_parse_tool_states_reads_doctor_tools():
    lines = ["   ok      rust2rpm   /usr/sbin/rust2rpm",
             "   absent  mock       mock (for mock-chain)",
             "   ok      copr-cli   /usr/bin/copr-cli",
             "   root    /tmp/packages (0 packages)"]
    assert rd.tui_results.parse_tool_states(lines) == {"rust2rpm": True, "mock": False, "copr-cli": True}
    assert rd.tui_results.parse_tool_states(["crate  packaged"]) == {}
    # the 'mock group' warning is a setup hint, not a missing tool
    lines = ["   ok      mock       /usr/bin/mock",
             "   WARNING mock group (sudo usermod -aG mock $USER, then log in again)",
             "   root    /tmp/packages (0 packages)"]
    assert rd.tui_results.parse_tool_states(lines) == {"mock": True}


def test_parse_columns_table_ignores_free_form_output():
    assert rd.tui_results.parse_columns_table(["== serde 1.0.217", "   rust2rpm --features default",
                                   "   ok", "NEW       serde 1.0  <- jsonschema"]) is None


def test_parse_state_table_reads_doctor_output():
    lines = ["   ok      rust2rpm   /usr/sbin/rust2rpm",
             "   absent  mock       mock (for mock-chain)",
             "   root    /tmp/packages (0 packages)"]
    assert rd.tui_results.parse_state_table(lines) == (
        ["state", "tool", "detail"],
        [["ok", "rust2rpm", "/usr/sbin/rust2rpm"],
         ["absent", "mock", "mock (for mock-chain)"],
         ["root", "/tmp/packages", "(0 packages)"]])


def test_parse_state_table_rejects_other_output():
    assert rd.tui_results.parse_state_table(["crate  packaged  crates.io", "serde  1.0  -"]) is None


def test_parse_json_table_shapes():
    assert rd.tui_results.parse_json_table([["serde", "syn"], ["jsonschema"]]) == (
        ["stage", "packages"], [["1", "serde syn"], ["2", "jsonschema"]])
    headers, rows = rd.tui_results.parse_json_table([{"crate": "serde", "needed_by": ["jsonschema"]},
                                         {"crate": "syn", "needed_by": []}])
    assert headers == ["crate", "needed_by"]
    assert rows == [["serde", '["jsonschema"]'], ["syn", "[]"]]
    assert rd.tui_results.parse_json_table({"a": 1, "b": None}) == (["key", "value"], [["a", "1"], ["b", ""]])
    assert rd.tui_results.parse_json_table([]) is None


@pytest.mark.parametrize("line, style", [
    ("ERROR: nope", "bold red"),
    ("ACTION NEEDED: serde: license", "bold magenta"),
    ("WARNING: x", "yellow"),
    ("== serde 1.0.217", "bold cyan"),
    ("=== stage 2", "bold cyan"),
    ("$ mock --chain ...", "dim"),
    ("NEW       serde 1.0.220  <- jsonschema", "cyan"),
    ("SYSTEM    serde 1.0.229             req ^1.0                <- core", "cyan"),
    ("WORKSPACE helper 1.0.0              req ^1.0.0              <- core", "cyan"),
    ("DROP      serde 1.0.200             Fedora has 1.0.229", "yellow"),
    ("   MISSING  fedora-44", "bold red"),
    ("   ok       fedora-rawhide", "green"),
    ("   BUILDING fedora-44-x86_64", "yellow"),
    ("   [!] some check", "bold red"),
    ("   [~] cargo metadata", "dim yellow"),
    ("summary: 3 ok, 1 failed", "bold"),
    ("plain line", ""),
])
def test_tui_line_style(line, style):
    assert rd.tui_app.tui_line_style(line) == style




# --- the TUI's lifecycle board, actions and settings -------------------------

@pytest.fixture
def tui_cache(tmp_path, monkeypatch):
    """Settings and logs go to a scratch cache, never the user's."""
    cache = tmp_path / ".cache"
    monkeypatch.setattr(rd.config, "CACHE_DIR", cache)
    return cache


def _static_text(widget) -> str:
    # Static keeps what it shows in .content (textual 8) or ._content (textual 4)
    shown = getattr(widget, "content", None) or getattr(widget, "_content", None)
    return str(shown or "")


async def _until(pilot, pred, steps: int = 600) -> bool:
    for _ in range(steps):
        if pred():
            return True
        await pilot.pause(0.05)
    return pred()


def _sheet_argv(sheet) -> list[str]:
    return rd.tui_form.tui_argv(sheet.fields, sheet.values())


def test_tui_board_groups_packages_in_pipeline_order(tmp_path):
    make_package(tmp_path, "a", "rust-a", "0.1.0")
    make_package(tmp_path, "b", "rust-b", "0.2.0")
    (tmp_path / "b" / "rust2rpm.toml").write_text("[tests]\n")
    (tmp_path / "b" / "rust-b-0.2.0-1.fc45.src.rpm").write_text("")
    board = rd.tui_overview.board(tmp_path)
    # every stage is listed, empty ones too, in the order a package walks them
    assert list(board) == rd.tui_overview.BOARD_ORDER
    assert set(board) == set(rd.tui_overview.STAGES)
    assert [p.crate for p in board["tests?"]] == ["a"]
    assert [p.crate for p in board["review"]] == ["b"]
    assert board["srpm?"] == []
    # each stage names a plain-language action that moves its packages on
    for stage in rd.tui_overview.TUI_STAGES:
        assert stage.action in rd.tui_actions.ACTIONS, stage.name
        assert stage.label and stage.blurb
        assert rd.tui_actions.STAGE_ACTIONS[stage.name][0] == stage.action


def test_tui_package_checklist_marks_where_a_package_is(tmp_path):
    make_package(tmp_path, "a", "rust-a", "0.1.0")
    pkg = rd.packages.local_packages(tmp_path)["a"]
    marks = dict((text, mark) for mark, text in rd.tui_overview.package_checklist(pkg))
    assert marks["spec generated"] == "✓"
    assert marks["no Cargo.toml patch"] == "–"  # neither done nor to do
    assert marks["tests chosen"] == "✗"  # the step it is at
    assert marks["SRPM 0.1.0"] == "·"  # not reached yet
    assert marks["review request filed"] == "·"
    (tmp_path / "a" / "rust2rpm.toml").write_text("[tests]\n")
    (tmp_path / "a" / "a-fix-metadata.diff").write_text("x")
    marks = dict((text, mark) for mark, text in rd.tui_overview.package_checklist(pkg))
    assert marks["tests chosen"] == "✓" and marks["Cargo.toml patch"] == "✓"
    assert marks["SRPM 0.1.0"] == "✗"
    # a target-limited package ends in COPR, not in a review request
    (tmp_path / "a" / rd.config.TARGETS_FILE).write_text('only = ["fedora-44"]\n')
    texts = [text for _, text in rd.tui_overview.package_checklist(pkg)]
    assert "COPR build" in texts and "review request filed" not in texts


def test_tui_stage_marks_unexplained_tests_tables(tmp_path):
    make_package(tmp_path, "a", "rust-a", "0.1.0")
    (tmp_path / "a" / "rust2rpm.toml").write_text(
        '[tests]\nrun = false\ncomments = [\n'
        '    "TODO: explain why doc is disabled (fails at runtime)",\n]\n')
    pkg = rd.packages.local_packages(tmp_path)["a"]
    assert rd.tui_overview.tui_stage(pkg).name == "tests-todo"
    assert rd.tui_overview.tests_todo_crates(tmp_path) == ["a"]
    # the stage's action is the in-UI reason editor, not another trial
    assert rd.tui_overview.STAGES["tests-todo"].action == "explain-skips"
    assert rd.tui_actions.ACTIONS["explain-skips"].cmd == ""
    # an explained table leaves the stage behind
    (tmp_path / "a" / "rust2rpm.toml").write_text(
        rd.tui_overview.replace_tests_comments((tmp_path / "a" / "rust2rpm.toml").read_text(),
                                  ["doc tests need network"]))
    assert rd.tui_overview.tui_stage(rd.packages.local_packages(tmp_path)["a"]).name == "srpm?"


def test_tui_actions_run_real_commands_and_flags():
    parsers = _subcommand_parsers()
    for key, a in rd.tui_actions.ACTIONS.items():
        assert a.verb and a.explain, key
        if not a.cmd:
            continue
        assert a.cmd in parsers, key
        keys = {f.key for f in rd.tui_form.tui_fields(parsers[a.cmd])}
        # an action only names fields its command has
        for k in (*a.flags, *a.preset, *a.inputs, *a.labels):
            assert k in keys, (key, k)
    for stage, keys in rd.tui_actions.STAGE_ACTIONS.items():
        assert stage in rd.tui_overview.STAGES
        assert all(k in rd.tui_actions.ACTIONS for k in keys), stage


@pytest.mark.parametrize("cmd, flags, key", [
    ("trial", [], "run-tests"),
    ("trial", ["discover"], "choose-tests"),
    ("trial", ["discover", "apply"], "apply-tests"),
    ("trial", ["apply", "discover", "online"], "apply-tests"),
    ("review-request", [], "review-request"),
    ("review-request", ["file"], "file-request"),
    ("copr", [], "copr"),  # --wait is a preset, not what makes it this action
    ("tests-edit", [], "explain-skips"),
])
def test_tui_action_for_a_command_line(cmd, flags, key):
    assert rd.tui_actions.action_for(cmd, flags).key == key


def test_tui_action_for_an_unknown_command_still_runs_it():
    a = rd.tui_actions.action_for("frobnicate", [])
    assert (a.cmd, a.verb) == ("frobnicate", "Run frobnicate")


def test_tui_settings_are_kept_per_root_in_the_cache(tmp_path, tui_cache):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()
    assert rd.tui_actions.load_settings(one) == rd.tui_actions.TuiSettings()
    rd.tui_actions.save_settings(one, rd.tui_actions.TuiSettings(project="u/p", chroots=["fedora-44-x86_64"]))
    rd.tui_actions.save_settings(two, rd.tui_actions.TuiSettings(fas="someone"))
    assert (tui_cache / rd.tui_actions.SETTINGS_FILE).exists()
    assert rd.tui_actions.load_settings(one).project == "u/p"
    assert rd.tui_actions.load_settings(one).chroots == ["fedora-44-x86_64"]
    assert rd.tui_actions.load_settings(two).fas == "someone"
    assert rd.tui_actions.load_settings(two).project == ""
    # a damaged file is no reason to fail: the UI starts from empty settings
    (tui_cache / rd.tui_actions.SETTINGS_FILE).write_text("{not json")
    assert rd.tui_actions.load_settings(one) == rd.tui_actions.TuiSettings()
    rd.tui_actions.save_settings(one, rd.tui_actions.TuiSettings(project="u/q"))
    assert rd.tui_actions.load_settings(one).project == "u/q"


def test_tui_settings_remember_only_what_was_unset():
    s = rd.tui_actions.TuiSettings(fas="me")
    assert rd.tui_actions.remember(s, {"project": " u/p ", "fas": "other", "chroot": ["fedora-44-x86_64"]})
    assert (s.project, s.fas, s.chroots) == ("u/p", "me", ["fedora-44-x86_64"])
    assert not rd.tui_actions.remember(s, {"project": "u/q", "chroot": "fedora-45-x86_64"})
    assert s.project == "u/p"


def test_tui_initial_values_take_settings_then_the_suggestion():
    parsers = _subcommand_parsers()
    s = rd.tui_actions.TuiSettings(project="u/p", chroots=["fedora-rawhide-x86_64", "fedora-44-x86_64"], fas="me")
    copr = rd.tui_form.tui_fields(parsers["copr"])
    v = rd.tui_actions.initial_values(rd.tui_actions.ACTIONS["copr"], copr, ["a", "b"], s)
    # a repeatable chroot gets every chroot; the preset --wait starts checked
    assert rd.tui_form.tui_argv(copr, v) == ["a", "b", "--project", "u/p", "--chroot", "fedora-rawhide-x86_64",
                                             "--chroot", "fedora-44-x86_64", "--wait"]
    mock = rd.tui_form.tui_fields(parsers["mock-chain"])
    v = rd.tui_actions.initial_values(rd.tui_actions.ACTIONS["mock-chain"], mock, ["a"], s)
    assert rd.tui_form.tui_argv(mock, v) == ["a", "--chroot", "fedora-rawhide-x86_64"]  # one chroot: the first
    # resolve/init's -r changes what counts as missing: settings do not fill it
    init = rd.tui_form.tui_fields(parsers["init"])
    v = rd.tui_actions.initial_values(rd.tui_actions.ACTIONS["init"], init, ["serde"], s)
    assert rd.tui_form.tui_argv(init, v) == ["serde", "--recursive"]
    # what a suggestion names wins over the settings; copr-log takes one package
    log = rd.tui_form.tui_fields(parsers["copr-log"])
    v = rd.tui_actions.initial_values(rd.tui_actions.action_for("copr-log"), log, [], s,
                                      values={"crate": "synta", "chroot": "fedora-44-x86_64"})
    assert rd.tui_form.tui_argv(log, v) == ["synta", "--chroot", "fedora-44-x86_64", "--project", "u/p",
                                            "--errors", "15"]


def test_tui_primary_keys_put_what_matters_first():
    parsers = _subcommand_parsers()
    copr = rd.tui_form.tui_fields(parsers["copr"])
    assert rd.tui_actions.primary_keys(rd.tui_actions.ACTIONS["copr"], copr) == [
        "crates", "all", "project", "chroot", "wait"]
    # whatever a command cannot run without is up front even if the action does not name it
    log = rd.tui_form.tui_fields(parsers["copr-log"])
    assert set(rd.tui_actions.primary_keys(rd.tui_actions.action_for("copr-log"), log)) >= {
        "crate", "chroot", "project"}


def test_tui_follow_ups_come_from_the_new_stages(tmp_path):
    make_package(tmp_path, "a", "rust-a", "0.1.0")
    make_package(tmp_path, "b", "rust-b", "0.2.0")
    (tmp_path / "b" / "rust2rpm.toml").write_text("[tests]\n")
    steps = rd.tui_actions.follow_ups(tmp_path, ["b", "a", "gone"])
    assert [(c, cr, fl) for c, cr, fl, _, _ in steps] == [
        ("trial", ["a"], ["discover"]),
        ("srpm", ["b"], []),
    ]
    # the action that just ran is not offered again for the packages it left in place
    assert [c for c, _, _, _, _ in rd.tui_actions.follow_ups(tmp_path, ["a", "b"], after="choose-tests")] == ["srpm"]
    merged = rd.tui_actions.merge_steps(steps, [("srpm", ["b"], [], {}, "again")])
    assert len(merged) == 2


def test_tui_action_needed_lines_are_collected():
    assert rd.tui_actions.action_needed(
        ["== a", "ACTION NEEDED: a: license file missing", "ok"]) == ["a: license file missing"]


def test_tui_app_board_opens_the_stage_action_with_packages_picked(tmp_path, tui_cache):
    pytest.importorskip("textual")
    app_class = rd.tui_app.make_tui_app()
    make_package(tmp_path, "a", "rust-a", "0.1.0")

    async def drive():
        app = app_class(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            board = app.screen
            assert type(board).__name__ == "BoardScreen"
            # it opens on the first stage with work in it, the package list focused
            assert board.stage == "tests?"
            assert app.focused is board.query_one("#pkgs")
            assert "Choose tests" in _static_text(board.query_one("#hint"))
            await pilot.press("enter")
            await pilot.pause()
            sheet = app.screen
            assert type(sheet).__name__ == "ActionSheet"
            assert sheet.action.key == "choose-tests"
            # the package arrives picked, the action's flag set, --timeout at its CLI default
            assert _sheet_argv(sheet) == ["a", "--discover", "--timeout", "900"]
            await pilot.press("escape")
            await pilot.pause()
            # d opens the package's own page, its next step first
            await pilot.press("d")
            await pilot.pause()
            assert type(app.screen).__name__ == "PackageScreen"
            assert app.screen.items[0] == ("action", "choose-tests")

    asyncio.run(drive())


def test_tui_app_gates_actions_on_missing_tools(tmp_path, tui_cache, monkeypatch):
    pytest.importorskip("textual")
    app_class = rd.tui_app.make_tui_app()
    make_package(tmp_path, "a", "rust-a", "0.1.0")
    monkeypatch.setattr(rd.tui_results, "parse_tool_states",
                        lambda lines: {"mock": False, "copr-cli": True,
                                       "fedora-review": False, "fedpkg": False})

    async def drive():
        app = app_class(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            assert await _until(pilot, lambda: app.tools_ok)  # the probe runs 'doctor'
            await pilot.pause()
            # the board says what is missing up front
            assert "missing: mock" in _static_text(app.screen.query_one("#topbar"))
            app.open_action("mock-chain", ["a"])
            await pilot.pause()
            # an action that needs a missing tool says what to install, and does not run
            assert app.screen.query_one("#run").disabled
            assert "dnf install mock" in _static_text(app.screen.query_one("#sheet-warning"))
            await pilot.press("escape")
            app.open_action("review", ["a"])
            await pilot.pause()
            assert "fedora-review" in _static_text(app.screen.query_one("#sheet-warning"))
            await pilot.press("escape")
            app.open_action("run-tests", ["a"])  # needs no optional tool
            await pilot.pause()
            assert not app.screen.query_one("#run").disabled
            assert not app.screen.query_one("#sheet-warning").display

    asyncio.run(drive())


def test_tui_app_sheet_folds_options_and_gates_on_required(tmp_path, tui_cache, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Collapsible, Input, Select
    app_class = rd.tui_app.make_tui_app()
    make_package(tmp_path, "a", "rust-a", "0.1.0")
    # Run is gated on the required inputs alone here: a host without copr-cli
    # (the CI container) would also gate it on the missing tool
    monkeypatch.setattr(rd.tui_results, "parse_tool_states", lambda lines: {})

    async def drive():
        app = app_class(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_action("copr", ["a"])
            await pilot.pause()
            sheet = app.screen
            more = sheet.query_one(Collapsible)
            assert more.collapsed
            # the inputs that matter are up front; the rest waits under More options
            for key in ("crates", "project", "chroot", "wait"):
                w = sheet.widgets.get(key) or sheet.multi[key][0]
                assert more not in w.ancestors, key
            for key in ("dry_run", "force", "retry_failed"):
                assert more in sheet.widgets[key].ancestors, key
            # labels are words, not option strings
            assert "COPR project *" in [_static_text(lbl) or str(lbl.render()) for lbl in sheet.query(".field-label")]
            # a required option gates Run and the preview says what is missing
            assert sheet.query_one("#run").disabled
            assert "still needed: COPR project" in _static_text(sheet.query_one("#preview"))
            sheet.widgets["project"].value = "u/p"
            await pilot.pause()
            assert not sheet.query_one("#run").disabled
            await pilot.press("escape")
            # 'regen' renders a mutually exclusive group as one choice
            app.open_action("regen", [])
            await pilot.pause()
            select = next(s for s in app.screen.query(Select) if s.name == "choice:1")
            select.value = "compat-2"
            await pilot.pause()
            assert _sheet_argv(app.screen) == ["--no-compat"]
            # and a value-taking mode shows its input only once chosen
            mode = next(s for s in app.screen.query(Select) if s.name == "choice:0")
            value = next(i for i in app.screen.query(Input) if i.name == "choicev:0")
            assert not value.display
            mode.value = "version"
            await pilot.pause()
            assert value.display
            value.value = "1.2.3"
            await pilot.pause()
            assert _sheet_argv(app.screen) == ["--version", "1.2.3", "--no-compat"]

    asyncio.run(drive())


def test_tui_app_repeatable_rows_stay_visible_and_aligned(tmp_path, tui_cache):
    """A repeatable argument is rows of inputs: each row keeps its height, its
    remove button sits beside the input, and + / – add and take rows away."""
    pytest.importorskip("textual")
    from textual.containers import Horizontal
    from textual.widgets import Button, Input
    app_class = rd.tui_app.make_tui_app()

    async def drive():
        app = app_class(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_action("workspace", [])
            await pilot.pause()
            sheet = app.screen
            # 'workspace' takes required positional paths: Run waits for one
            assert sheet.query_one("#run").disabled
            rows = sheet.multi["projects"]
            assert len(rows) == 1
            field = rows[0]
            row = field.parent
            assert isinstance(row, Horizontal)
            remove = row.query_one(Button)
            assert field.content_region.height >= 1, "the input shows no text"
            assert row.region.height == rd.tui_app.FORM_ROW_HEIGHT
            assert field.region.y == remove.region.y == row.region.y
            # a path with a space in it stays one argument
            app.set_focus(field)
            await pilot.press(*str(tmp_path / "my ws"))
            assert _sheet_argv(sheet) == [str(tmp_path / "my ws")]
            assert not sheet.query_one("#run").disabled
            add = next(b for b in sheet.query(Button) if b.name == "add:projects")
            add.press()
            await pilot.pause()
            holder = row.parent
            assert len(holder.query(Input)) == 2
            assert holder.region.height == 2 * rd.tui_app.FORM_ROW_HEIGHT
            assert holder.query(Input).last().content_region.height >= 1
            remove.press()
            await pilot.pause()
            assert len(holder.query(Input)) == 1
            assert holder.region.height == rd.tui_app.FORM_ROW_HEIGHT

    asyncio.run(drive())


def test_tui_app_runs_a_command_and_summarizes_it(tmp_path, tui_cache, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import ContentSwitcher, DataTable
    app_class = rd.tui_app.make_tui_app()
    make_package(tmp_path, "a", "rust-a", "0.1.0")
    monkeypatch.setattr(rd.tui_results, "parse_tool_states", lambda lines: {})

    async def drive():
        app = app_class(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # every command is reachable through "All commands"
            await pilot.press("question_mark")
            await pilot.pause()
            menu = app.screen
            listed = {menu.query_one("OptionList").get_option_at_index(i).id
                      for i in range(menu.query_one("OptionList").option_count)}
            assert set(_subcommand_parsers()) - {"tui"} <= listed
            menu.dismiss("doctor")
            await pilot.pause()
            assert app.screen.action.cmd == "doctor"
            await pilot.press("ctrl+r")
            await pilot.pause()
            run = app.screen
            assert type(run).__name__ == "RunScreen"
            assert await _until(pilot, lambda: not run.running), "the command did not finish"
            await pilot.pause()
            # the run ends on its summary: the verdict and doctor's state lines as a table
            assert run.query_one("#run-view", ContentSwitcher).current == "run-summary"
            assert run.query_one("#result-table", DataTable).row_count >= 3
            assert "Check setup" in _static_text(run.query_one("#verdict"))
            await pilot.press("l")
            assert run.query_one("#run-view", ContentSwitcher).current == "run-log"
            await pilot.press("escape")
            await pilot.pause()
            assert type(app.screen).__name__ == "BoardScreen"

    asyncio.run(drive())


async def _finished_run(app, pilot, cmd: str, flags: list[str], crates: list[str], lines: list[str], code=0):
    """A RunScreen showing what 'lines' would have printed (its own run is '--help')."""
    screen = type(app).RunScreen(rd.tui_actions.action_for(cmd, flags), ["--help"], crates)
    app.push_screen(screen)
    assert await _until(pilot, lambda: not screen.running)
    screen.stdout, screen.lines = list(lines), list(lines)
    screen.finish(code)
    await pilot.pause()
    return screen


def test_tui_app_trial_results_drive_next_steps(tmp_path, tui_cache):
    pytest.importorskip("textual")
    from textual.widgets import DataTable, OptionList
    app_class = rd.tui_app.make_tui_app()
    make_package(tmp_path, "synta", "rust-synta", "0.4.0")
    trial_lines = [
        "== trial synta 0.4.0 (log: /home/u/.cache/rust-packaging-tools/trial/synta.log)",
        "   all                      FAILED  0 passed, 0 failed",
        "      error[E0432]: unresolved import `synta_certificate`",
        "Suggested [tests] table for rust2rpm.toml (replace the TODO comments with real reasons):",
        "[tests]",
        "run = []",
        "ACTION NEEDED: synta: tests need synta-certificate",
    ]

    async def drive():
        app = app_class(tmp_path)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            run = await _finished_run(app, pilot, "trial", ["discover"], ["synta"], trial_lines, code=1)
            # the verdict lines become a table, the problems are spelled out
            assert run.query_one("#result-table", DataTable).row_count == 1
            verdict = _static_text(run.query_one("#verdict"))
            assert "failed" in verdict and "tests need synta-certificate" in verdict
            # the suggested [tests] table is the first next step: write it
            cmd, crates, flags, _, _ = run.steps[0]
            assert (cmd, crates, flags) == ("trial", ["synta"], ["discover", "apply"])
            assert rd.tui_actions.action_for(cmd, flags).verb == "Write the [tests] table"
            nxt = run.query_one("#next", OptionList)
            run.on_option_list_option_selected(OptionList.OptionSelected(nxt, nxt.get_option_at_index(0), 0))
            await pilot.pause()
            sheet = app.screen
            assert sheet.action.key == "apply-tests"
            assert sheet.widgets["discover"].value is True and sheet.widgets["apply"].value is True
            assert _sheet_argv(sheet)[0] == "synta"

    asyncio.run(drive())


def test_tui_app_srpm_results_render_as_a_table(tmp_path, tui_cache):
    pytest.importorskip("textual")
    from textual.widgets import DataTable
    app_class = rd.tui_app.make_tui_app()
    make_package(tmp_path, "a", "rust-a", "0.1.0")
    srpm_lines = [
        "== a 0.1.0",
        "   Wrote: /root/a/a-0.1.0-1.fc45.src.rpm",
        "   %prep ok",
        '   1 packages and 0 specfiles checked; 0 errors, 0 warnings, 0 filtered, 0 aborted, rating: "OK"',
    ]

    async def drive():
        app = app_class(tmp_path)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            run = await _finished_run(app, pilot, "srpm", [], ["a"], srpm_lines)
            table = run.query_one("#result-table", DataTable)
            assert table.display and table.row_count == 1
            # rebuilding the summary does not stack columns or rows
            run.finish(0)
            await pilot.pause()
            assert table.row_count == 1 and len(table.columns) == 5
            # output with no table shape shows no empty table
            run.stdout = run.lines = ["nothing structured"]
            run.finish(0)
            await pilot.pause()
            assert not table.display
            # the package's next step comes from its stage even when the tool proposes none
            assert run.steps and run.steps[0][0] == "trial"

    asyncio.run(drive())


def test_tui_app_tool_proposed_steps_become_actions(tmp_path, tui_cache):
    pytest.importorskip("textual")
    from textual.widgets import OptionList
    app_class = rd.tui_app.make_tui_app()
    make_package(tmp_path, "synta-derive", "rust-synta-derive", "0.4.0")
    plan_lines = [
        "Submit in this order: a ticket names the tickets of the packages it waits for.",
        "",
        "=== stage 1",
        "  1. rust-synta-derive 0.4.0  NOT READY",
        "       no local fedora-review result; run 'review' first",
        "",
        "summary: 1 not-ready",
    ]

    async def drive():
        app = app_class(tmp_path)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            run = await _finished_run(app, pilot, "review-plan", [], ["synta-derive"], plan_lines)
            assert run.query_one("#result-table").row_count == 1
            assert [(c, cr, fl) for c, cr, fl, _, _ in run.steps][0] == ("review", ["synta-derive"], [])
            nxt = run.query_one("#next", OptionList)
            run.on_option_list_option_selected(OptionList.OptionSelected(nxt, nxt.get_option_at_index(0), 0))
            await pilot.pause()
            assert app.screen.action.key == "review"
            assert _sheet_argv(app.screen)[0] == "synta-derive"

    asyncio.run(drive())


def test_tui_app_explains_test_skips_and_regenerates(tmp_path, tui_cache):
    pytest.importorskip("textual")
    from textual.widgets import Input
    app_class = rd.tui_app.make_tui_app()
    make_package(tmp_path, "a", "rust-a", "0.1.0")
    (tmp_path / "a" / "rust2rpm.toml").write_text(
        '[tests]\nrun = false\ncomments = [\n'
        '    "TODO: explain why doc is disabled (fails at runtime)",\n]\n')

    async def drive():
        app = app_class(tmp_path)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            assert app.screen.stage == "tests-todo"
            await pilot.press("enter")
            await pilot.pause()
            editor = app.screen
            assert type(editor).__name__ == "ExplainScreen"
            # the TODO arrives editable, the table itself shown as context
            assert len(editor.query(Input)) == 1
            assert any("run = false" in _static_text(s) for s in editor.query("Static"))
            editor.query(Input).first().value = "doc tests need network; %cargo_test skips them"
            editor.action_save()
            await pilot.pause()
            text = (tmp_path / "a" / "rust2rpm.toml").read_text()
            assert "TODO" not in text and "doc tests need network" in text and "run = false" in text
            # saving goes straight on to regenerate the spec for those packages
            run = app.screen
            assert type(run).__name__ == "RunScreen"
            assert (run.action.key, run.argv) == ("regen", ["a"])
            assert await _until(pilot, lambda: not run.running)
            pkg = rd.packages.local_packages(tmp_path)["a"]
            assert rd.tui_overview.tui_stage(pkg).name == "srpm?"

    asyncio.run(drive())


def test_tui_app_onboarding_routes_crates_and_projects(tmp_path, tui_cache):
    pytest.importorskip("textual")
    from textual.widgets import Input
    app_class = rd.tui_app.make_tui_app()
    project = tmp_path / "proj"
    project.mkdir()

    async def drive():
        app = app_class(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # an empty root opens on onboarding and says what to do
            board = app.screen
            assert "Nothing packaged here yet" in _static_text(board.query_one("#stage-title"))
            await pilot.press("n")
            await pilot.pause()
            new = app.screen
            assert app.focused is new.query_one("#what", Input)
            await pilot.press(*"serde_json_path jsonschema@^0.30")
            assert new.plan() == ("resolve", ["serde_json_path", "jsonschema@^0.30"], {})
            new.query_one("#what", Input).value = str(project)
            await pilot.pause()
            assert new.plan() == ("workspace", [], {"projects": str(project)})
            assert "Audit a Rust project" in _static_text(new.query_one("#new-plan"))
            # More options opens the full sheet with the project filled in
            new.start("more")
            await pilot.pause()
            assert app.screen.action.key == "workspace"
            assert _sheet_argv(app.screen) == [str(project)]

    asyncio.run(drive())
