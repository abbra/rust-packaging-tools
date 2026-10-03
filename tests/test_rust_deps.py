"""Unit tests for rust-deps: offline, no rust2rpm runs, no network.

rust-deps is a script without a .py suffix; load it as a module.
"""

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import urllib.error
import tomllib
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "fedora-rust-packaging" / "scripts" / "rust-deps"
_loader = importlib.machinery.SourceFileLoader("rust_deps", str(SCRIPT))
rd = importlib.util.module_from_spec(importlib.util.spec_from_loader("rust_deps", _loader))
sys.modules["rust_deps"] = rd  # dataclasses look up their module while it is being executed
_loader.exec_module(rd)


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
    assert rd.req_matches(req, version) is expected


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
    assert rd.is_foreign(target) is foreign


def test_feature_closure():
    features = {"default": ["std", "derive"], "std": [], "derive": ["dep:synta-derive", "serde?/derive"],
                "tls": ["rustls/ring"]}
    feats, deps = rd.feature_closure(features, {"default"})
    assert feats == {"default", "std", "derive"}
    assert "synta-derive" in deps and "serde" not in deps  # "serde?/derive" does not enable serde


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
    assert rd.chroot_metalinks(chroot) == repos


def test_chroot_release_and_mock_chroot():
    assert rd.chroot_release("rhel+epel-10-aarch64") == "rhel+epel-10"
    assert rd.mock_chroot("rhel+epel-10-x86_64") == "centos-stream+epel-10-x86_64"
    assert rd.mock_chroot("fedora-44-x86_64") == "fedora-44-x86_64"


def test_chroot_metalinks_unknown():
    with pytest.raises(SystemExit):
        rd.chroot_metalinks("opensuse-tumbleweed-x86_64")



# ─── status columns ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("target, chroot", [
    ("fedora-44", "fedora-44-x86_64"),
    ("fedora-rawhide", "fedora-rawhide-x86_64"),
    ("rhel+epel-10", "rhel+epel-10-x86_64"),
    ("epel-10", "epel-10-x86_64"),
    ("fedora-45-aarch64", "fedora-45-aarch64"),
])
def test_release_chroot(target, chroot):
    assert rd.release_chroot(target) == chroot


def test_release_chroot_epel_flavours():
    # COPR's rhel+epel-N uses EPEL for the current RHEL minor; plain epel-N is EPEL itself
    assert rd.chroot_metalinks(rd.release_chroot("rhel+epel-10")) == ["epel-z-10"]
    assert rd.chroot_metalinks(rd.release_chroot("epel-10")) == ["epel-10"]


@pytest.mark.parametrize("versions, local, all_versions, cell", [
    ([], "1.0.0", False, "-"),
    (["1.0.21"], "1.0.23", False, "1.0.21"),
    (["0.16.1", "0.17.1"], "0.17.1", False, "0.17.1= (+1)"),
    (["0.16.1", "0.17.1"], "0.16.1", False, "0.17.1 (+1)"),
    (["0.16.1", "0.17.1"], "0.16.1", True, "0.16.1=,0.17.1"),
])
def test_version_cell(versions, local, all_versions, cell):
    assert rd.version_cell(versions, local, all_versions) == cell


def test_host_release(tmp_path, monkeypatch):
    osr = tmp_path / "os-release"
    osr.write_text('NAME="Fedora Linux"\nID=fedora\nVERSION_ID=45\n')
    monkeypatch.delenv("RUST_DEPS_DNF_ARGS", raising=False)
    assert rd.host_release(osr) == "fedora-45"
    monkeypatch.setenv("RUST_DEPS_DNF_ARGS", "--releasever=rawhide")
    assert rd.host_release(osr) == "fedora-45 (--releasever=rawhide)"
    assert rd.host_release(tmp_path / "missing") == "host (--releasever=rawhide)"


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
    return tomllib.loads(rd.apply_edits(text, edits))


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
    comments = rd.describe_edits({"drop-features": ["magnus"]})
    assert rd.CHECK_CFG_COMMENT in comments


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
    pkgs = rd.local_packages(tmp_path)
    assert set(pkgs) == {"zmij", "hashbrown"}
    assert not pkgs["zmij"].compat and pkgs["zmij"].rpm_name == "rust-zmij"
    assert pkgs["hashbrown"].compat and pkgs["hashbrown"].spec.name == "rust-hashbrown0.17.spec"
    assert pkgs["hashbrown"].version == "0.17.1"


def test_targets_toml(tmp_path):
    d = make_package(tmp_path, "serde_assert", "rust-serde_assert", "0.8.0")
    (d / rd.TARGETS_FILE).write_text('only = ["fedora-44", "rhel+epel-10"]\n')
    pkg = rd.local_packages(tmp_path)["serde_assert"]
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
    assert rd.unsafe_skips(tests) == bad


@pytest.mark.parametrize("skip, bad", [
    ("src/lib.rs - (line 15)", ["src/lib.rs - (line 15)"]),
    ("tests::ok", []),
])
def test_unsafe_skips_accepts_a_lone_string(skip, bad):
    assert rd.unsafe_skips({"skip": skip}) == bad


def test_spec_invocations_with_a_string_skip(tmp_path):
    d = make_package(tmp_path, "smol", "rust-smol", "2.0.2")
    (d / "rust2rpm.toml").write_text('[tests]\nskip = "tests::needs_network"\n')
    assert rd.spec_invocations(rd.local_packages(tmp_path)["smol"]) == \
        [("all", ["--", "--skip", "tests::needs_network"])]


def test_trial_refuses_unsafe_skips(tmp_path, capsys):
    d = make_package(tmp_path, "smol", "rust-smol", "2.0.2")
    (d / "rust2rpm.toml").write_text('[tests]\nrun = ["doc"]\nskip.doc = ["src/lib.rs - (line 15)"]\n')
    assert rd.trial(rd.local_packages(tmp_path)["smol"], discover=False) is False
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
    assert rd.foreign_shebangs(crate, {}) == [("wasm/emscripten/runner.py", "/usr/local/bin/python")]
    # removed (or made non-executable) by the prep scripts: handled
    handled = {"scripts": {"prep": {"post": ["rm -r newsfragments/ wasm/"]}}}
    assert rd.foreign_shebangs(crate, handled) == []


# ─── updates to a new upstream release ──────────────────────────────────────

def stub_buildrequires(monkeypatch, brs: dict[str, list[str]]):
    """BuildRequires lines per crate, as build_info would return them."""
    monkeypatch.setattr(rd, "build_info", lambda pkg: ({}, brs.get(pkg.crate, [])))


def crate_dep(name: str, req: str, kind: str = "normal", optional: bool = False) -> dict:
    """A dependency as the crates.io API shapes it, for resolve."""
    return {"name": name, "crate_id": name, "req": req, "optional": optional,
            "default_features": True, "features": [], "kind": kind, "target": None}


def stub_cratesio(monkeypatch, versions: dict[str, list[str]], deps: dict[str, list[dict]]):
    monkeypatch.setattr(rd, "crate_versions",
                        lambda name: [{"num": v, "yanked": False} for v in versions.get(name, [])])
    monkeypatch.setattr(rd, "crate_dependencies", lambda name, version: deps.get(name, []))


def test_build_stages_orders_by_local_dependencies(tmp_path, monkeypatch):
    for crate in ("leaf", "mid", "top"):
        make_package(tmp_path, crate, f"rust-{crate}")
    stub_buildrequires(monkeypatch, {
        "mid": ["crate(leaf) >= 0.1"],
        "top": ["crate(mid) >= 0.1", "crate(leaf) >= 0.1", "crate(serde) >= 1.0"],  # serde: Fedora has it
    })
    pkgs = list(rd.local_packages(tmp_path).values())
    stages = rd.build_stages(pkgs, tmp_path)
    assert [[p.crate for p in s] for s in stages] == [["leaf"], ["mid"], ["top"]]


def test_build_stages_detects_a_cycle(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, "a", "rust-a")
    make_package(tmp_path, "b", "rust-b")
    stub_buildrequires(monkeypatch, {"a": ["crate(b) >= 0.1"], "b": ["crate(a) >= 0.1"]})
    with pytest.raises(SystemExit):
        rd.build_stages(list(rd.local_packages(tmp_path).values()), tmp_path)
    assert "dependency cycle" in capsys.readouterr().err


def test_build_stages_warns_about_dependencies_outside_the_selection(tmp_path, monkeypatch, capsys):
    make_package(tmp_path, "a", "rust-a")
    make_package(tmp_path, "b", "rust-b")
    stub_buildrequires(monkeypatch, {"a": ["crate(b) >= 0.1"]})
    stages = rd.build_stages([rd.local_packages(tmp_path)["a"]], tmp_path)
    assert [[p.crate for p in s] for s in stages] == [["a"]]
    assert "not in this build: b" in capsys.readouterr().err


def test_resolve_new_with_transitive_deps(monkeypatch):
    stub_cratesio(monkeypatch, {"top": ["2.0.0"], "mid": ["1.5.0"]},
                  {"top": [crate_dep("mid", "^1.0")], "mid": []})
    needed = rd.resolve([("top", "*", "(test)", ["default"])], rd.FedoraIndex({}), {})
    assert {k: n.status for k, n in needed.items()} == {"top@2.0.0": "new", "mid@1.5.0": "new"}
    assert needed["mid@1.5.0"].needed_by == {"top"}


def test_resolve_update_when_fedora_has_another_version(monkeypatch):
    stub_cratesio(monkeypatch, {"mid": ["1.5.0"]}, {"mid": []})
    needed = rd.resolve([("mid", "^1.2", "(test)", ["default"])], rd.FedoraIndex({"mid": {"1.0.0": {""}}}), {})
    assert needed["mid@1.5.0"].status == "update"
    assert needed["mid@1.5.0"].fedora_versions == ["1.0.0"]


def test_resolve_features_when_fedora_version_lacks_a_feature(monkeypatch):
    fedora = rd.FedoraIndex({"mid": {"1.5.0": {""}}})  # ships the crate, not the feature
    needed = rd.resolve([("mid", "^1.2", "(test)", ["derive"])], fedora, {})
    assert needed["mid@1.5.0"].status == "features"
    assert needed["mid@1.5.0"].missing_features == ["derive"]


def test_resolve_counts_a_matching_local_package(tmp_path, monkeypatch):
    stub_cratesio(monkeypatch, {"top": ["2.0.0"]}, {"top": [crate_dep("mid", "^1.0")]})
    make_package(tmp_path, "mid", "rust-mid", "1.5.0")
    needed = rd.resolve([("top", "*", "(test)", ["default"])], rd.FedoraIndex({}), rd.local_packages(tmp_path))
    assert set(needed) == {"top@2.0.0"}  # mid is covered by the local package


def test_crate_versions_treats_404_as_no_such_crate(monkeypatch):
    def not_found(url, key, max_age):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(rd, "_cached_json", not_found)
    rd.crate_versions.cache_clear()
    assert rd.crate_versions("nosuchcrate12345") == []
    rd.crate_versions.cache_clear()


def releases(monkeypatch, *nums):
    monkeypatch.setattr(rd, "crate_versions", lambda name: [{"num": n, "yanked": False} for n in nums])


def test_update_version(tmp_path, monkeypatch):
    make_package(tmp_path, "native-ossl", "rust-native-ossl", "0.3.0")
    make_package(tmp_path, "hashbrown", "rust-hashbrown0.15", "0.15.2")
    pkgs = rd.local_packages(tmp_path)
    releases(monkeypatch, "0.16.0", "0.15.5", "0.3.1", "0.3.0", "0.2.0")
    assert rd.update_version(pkgs["native-ossl"], None) == ("0.16.0", None)
    assert rd.update_version(pkgs["native-ossl"], "0.3.1") == ("0.3.1", None)
    assert rd.update_version(pkgs["native-ossl"], "0.3.0")[1] == "already at 0.3.0"
    assert "older" in rd.update_version(pkgs["native-ossl"], "0.2.0")[1]
    assert "not on crates.io" in rd.update_version(pkgs["native-ossl"], "0.4.0")[1]
    # a compat package stays in its series unless told otherwise
    assert rd.update_version(pkgs["hashbrown"], None) == ("0.15.5", None)
    assert "--no-compat" in rd.update_version(pkgs["hashbrown"], "0.16.0")[1]


def test_repin_upstream_sources(tmp_path, monkeypatch):
    old, new = "379bedbd040ff060f712f54377d41cb59e094f2b", "60f49306515247ae43aac5b370aaffc6e2abc741"
    d = make_package(tmp_path, "ring-native-ossl", "rust-ring-native-ossl", "0.3.0")
    url = "https://forge.fedoraproject.org/freeipa/native-ossl/raw/commit/{}/LICENSE"
    (d / "rust2rpm.toml").write_text(f'[[package.extra-sources]]\nnumber = 10\nfile = "{url.format(old)}"\n')
    pkg = rd.local_packages(tmp_path)["ring-native-ossl"]
    monkeypatch.setattr(rd, "url_exists", lambda u: False)
    assert rd.repin_upstream_sources(pkg, old, new) == [url.format(new)]
    assert old not in pkg.config_file.read_text() and new in pkg.config_file.read_text()
    assert rd.repin_upstream_sources(pkg, old, new) == []  # nothing pinned to the old commit any more


def test_edits_drift():
    toml = {"dependencies": {"serde": {"version": "1", "optional": True}},
            "dev-dependencies": {"criterion": "0.5", "proptest": "1"}, "features": {"serde": ["dep:serde"]}}
    edits = {"drop-dev-dependencies": ["criterion", "quickcheck"], "set-dev-version": {"proptest": "1.4"}}
    suggested = {"drop-dev-dependencies": ["criterion", "proptest"], "drop-dependencies": ["serde"],
                 "drop-features": ["serde"]}
    new, stale = rd.edits_drift(toml, edits, suggested)
    assert new == ["drop-dependencies: serde", "drop-features: serde"]  # proptest is handled by set-dev-version
    assert stale == ["drop-dev-dependencies: quickcheck"]


def test_broken_dependents(tmp_path):
    d = make_package(tmp_path, "freeipa", "rust-freeipa", "0.1.0")
    crate = make_crate(tmp_path, {"Cargo.toml": (
        '[package]\nname = "freeipa"\n[dependencies.native-ossl]\nversion = "^0.3"\n'
        '[dev-dependencies.native-ossl-sys]\nversion = "=0.3.0"\n', 0o644)})
    crate.rename(d / "freeipa-0.1.0.crate")
    make_package(tmp_path, "native-ossl", "rust-native-ossl", "0.4.0")
    assert rd.broken_dependents({"native-ossl": "0.3.1", "native-ossl-sys": "0.3.1"}, tmp_path) == \
        ["freeipa 0.1.0 needs native-ossl-sys =0.3.0 (dev)"]
    assert rd.broken_dependents({"native-ossl": "0.4.0"}, tmp_path) == ["freeipa 0.1.0 needs native-ossl ^0.3 (normal)"]


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
    m = rd.MISSING_RE.search(line)
    assert m and rd.MissingReq.parse(m.group(1) or m.group(2)).label() == label


def test_conflict_parsing():
    line = ("  - cannot install both rust-hashbrown-devel-0.16.1-1.el10_2.noarch from epel and "
            "rust-hashbrown-devel-0.17.1-1.el10.noarch from copr_base")
    [c] = rd.log_conflicts([line, line])
    assert c.crate == "hashbrown"
    assert c.sides == [("0.16.1", "epel"), ("0.17.1", "copr_base")]


def local_pkg(tmp_path: Path, drop_features: list[str]) -> "rd.LocalPackage":
    d = tmp_path / "jsonschema"
    d.mkdir(exist_ok=True)
    (d / rd.EDITS_FILE).write_text(f"drop-features = {drop_features!r}\n".replace("'", '"'))
    return rd.LocalPackage("jsonschema", d, "0.58.0")


def test_explain_warning(tmp_path):
    pkg = local_pkg(tmp_path, ["macros"])
    assert "drops feature 'macros'" in rd.explain_warning("unexpected `cfg` condition value: `macros`", pkg)
    assert "upstream lint" in rd.explain_warning("unexpected `cfg` condition value: `other`", pkg)
    assert "rust2rpm" in rd.explain_warning("File listed twice: /usr/share/cargo/registry/x-1.0/LICENSE", pkg)
    assert rd.explain_warning("unused variable: `x`", pkg) is None


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
    missing, errors, warnings = rd.summarize_log(log, pkg)
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
    return rd.TmtInfo(**{**base, **kw})


def fmf(text: str) -> dict:
    return yaml.safe_load(text)


def test_render_tmt_library(tmp_path):
    files = rd.render_tmt(tmt_info())
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
    files = rd.render_tmt(tmt_info(check_commands=[], test_requires=[]))
    assert "/upstream-tests" not in fmf(files["tests/rust-deps/main.fmf"])
    assert "tests/rust-deps/check-commands" not in files
    app = rd.render_tmt(tmt_info(devel=[], features=[], binaries=["synta-tools"], check_commands=[]))
    assert set(fmf(app["tests/rust-deps/main.fmf"])) >= {"/executables"}
    assert "/features" not in fmf(app["tests/rust-deps/main.fmf"])


def test_write_tmt_replaces_earlier_files(tmp_path, monkeypatch):
    pkg = rd.LocalPackage("zmij", tmp_path, "1.0.23")
    stale = tmp_path / "tests" / "rust-deps" / "executables.sh"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    monkeypatch.setattr(rd, "tmt_info", lambda p: tmt_info())
    rd.write_tmt(pkg)
    assert not stale.exists()
    assert (tmp_path / "tests/rust-deps/features.sh").stat().st_mode & 0o111
    assert (tmp_path / "plans/rust-deps.fmf").exists()


@pytest.mark.parametrize("chroot, image", [
    ("fedora-45-x86_64", "registry.fedoraproject.org/fedora:45"),
    ("fedora-rawhide-aarch64", "registry.fedoraproject.org/fedora:rawhide"),
])
def test_tmt_image(chroot, image):
    assert rd.tmt_image(chroot) == image


# ─── review plan ────────────────────────────────────────────────────────────

def test_review_plan(tmp_path, monkeypatch):
    import os, json as _json
    pkgs = {}
    for crate, spec_name in [("zmij", "rust-zmij"), ("synta-derive", "rust-synta-derive"),
                             ("synta", "rust-synta"), ("synta-cbor", "rust-synta-cbor"),
                             ("ciborium", "rust-ciborium")]:
        make_package(tmp_path, crate, spec_name, "1.0.0")
    (tmp_path / "ciborium" / rd.TARGETS_FILE).write_text('only = ["rhel+epel-10"]\n')
    (tmp_path / "zmij" / rd.DIST_GIT_FILE).write_text('package = "rust-zmij"\nbranch = "rawhide"\ncommit = "2cccfd47b7"\n')
    (tmp_path / "synta-cbor" / rd.REQUEST_STATE).write_text(_json.dumps({"bug": 2600001}))
    draft = tmp_path / "synta-derive" / rd.REQUEST_DRAFT
    draft.write_text("Summary: Review Request: rust-synta-derive - derive macros\n")
    os.utime(draft, (2_000_000_000, 2_000_000_000))  # newer than the spec
    local = rd.local_packages(tmp_path)
    stages = [[local["zmij"], local["synta-derive"], local["ciborium"]], [local["synta"]], [local["synta-cbor"]]]
    monkeypatch.setattr(rd, "build_stages", lambda pkgs, root, warn_outside=True: stages)
    monkeypatch.setattr(rd, "local_deps", lambda pkgs, root: {
        "synta": {"synta-derive", "zmij"}, "synta-cbor": {"synta", "ciborium"}})
    monkeypatch.setattr(rd, "dist_git_has", lambda name: name == "rust-zmij")
    monkeypatch.setattr(rd, "fedora_index", lambda refresh=False, chroot=None: rd.FedoraIndex({"zmij": {"0.9.0": {""}}}))
    monkeypatch.setattr(rd, "srpm_of", lambda pkg: pkg.dir / "x.src.rpm")
    monkeypatch.setattr(rd, "local_review_problem", lambda pkg: None if pkg.crate == "synta-derive"
                        else "no local fedora-review result; run 'review' first")
    plan = rd.review_plan(list(local.values()), tmp_path, None, "fedora-rawhide-x86_64")
    by = {i.pkg.crate: i for s in plan for i in s}
    assert (by["zmij"].kind, by["ciborium"].kind) == ("update", "skip")
    assert "Rawhide: 0.9.0" in by["zmij"].detail and "adopted from dist-git rawhide 2cccfd47b7" in by["zmij"].detail
    (tmp_path / "zmij" / rd.DIST_GIT_FILE).write_text('package = "rust-zmij"\nbranch = "rawhide"\ncommit = "2cccfd47b7"\n'
                                                      'packaging = "local"\n')
    plan = rd.review_plan(list(local.values()), tmp_path, None, "fedora-rawhide-x86_64")
    marked = {i.pkg.crate: i for s in plan for i in s}
    assert marked["zmij"].kind == "skip" and "marked from dist-git" in marked["zmij"].detail
    assert marked["synta"].after == ["rust-synta-derive"]  # a marked package is not waited for
    assert (by["synta-derive"].state, by["synta-derive"].detail) == ("ready", str(draft))
    assert by["synta"].state == "not-ready" and by["synta"].after == ["rust-synta-derive", "rust-zmij"]
    assert by["synta-cbor"].state == "filed" and by["synta-cbor"].detail.endswith("/2600001")
    assert by["synta-cbor"].after == ["rust-synta"]  # ciborium is not reviewed here
    os.utime(draft, (1, 1))  # a draft older than the spec does not count
    plan = rd.review_plan(list(local.values()), tmp_path, None, "fedora-rawhide-x86_64")
    assert [i.state for s in plan for i in s if i.pkg.crate == "synta-derive"] == ["not-ready"]



# ─── adopted from Fedora's dist-git ─────────────────────────────────────────

def test_dist_git_files():
    tree = [{"name": n, "type": t} for n, t in [
        (".gitignore", "file"), ("0001-fix.patch", "file"), ("rust-zmij.spec", "file"), ("rust2rpm.toml", "file"),
        ("sources", "file"), ("changelog", "file"), ("zmij-fix-metadata.diff", "file"), ("plans", "dir")]]
    assert rd.dist_git_files(tree, "rust-zmij") == ["0001-fix.patch", "rust2rpm.toml", "zmij-fix-metadata.diff"]


def test_spec_similarity():
    spec = "# Generated by rust2rpm 28\nName: rust-x\nVersion: 1.0\n\n%changelog\n* one\n"
    assert rd.spec_similarity(spec, spec.replace("rust2rpm 28", "rust2rpm 27").replace("* one", "* two")) == (0, 2)
    assert rd.spec_similarity(spec, spec.replace("1.0", "1.1"))[0] == 2


def test_adopted_package_builds_where_missing(tmp_path, monkeypatch):
    d = make_package(tmp_path, "serde_assert", "rust-serde_assert", "0.8.0")
    (d / rd.TARGETS_FILE).write_text('only = ["fedora-45"]\n')  # ignored once adopted
    (d / rd.DIST_GIT_FILE).write_text('package = "rust-serde_assert"\nbranch = "rawhide"\n')
    have = {"fedora-44": {}, "fedora-rawhide": {"0.8.0": {""}}, "rhel+epel-10": {"0.7.0": {""}}}
    monkeypatch.setattr(rd, "target_index", lambda chroot, refresh=False:
                        rd.FedoraIndex({"serde_assert": have[rd.chroot_release(chroot)]}))
    pkg = rd.local_packages(tmp_path)["serde_assert"]
    assert pkg.limited and "in Fedora" in pkg.scope
    assert pkg.builds_in("fedora-44-x86_64") and pkg.builds_in("rhel+epel-10-aarch64")
    assert not pkg.builds_in("fedora-rawhide-x86_64")


# ─── dist-git updates ───────────────────────────────────────────────────────

def test_update_message(tmp_path):
    d = make_package(tmp_path, "zmij", "rust-zmij", "1.0.23")
    (d / "rust2rpm.toml").write_text('[package]\ncargo-toml-patch-comments = ["drop opt-level", "relax num-bigint"]\n')
    pkg = rd.local_packages(tmp_path)["zmij"]
    old_config = '[package]\ncargo-toml-patch-comments = ["drop opt-level"]\n'
    msg = rd.update_message(pkg, "Version:        1.0.21\n", old_config, ["plans/rust-deps.fmf"])
    assert msg == ("Update to 1.0.23\n\n- relax num-bigint\n"
                   "- add tmt tests for Fedora CI (generated by rust-deps)\n")
    assert rd.update_message(pkg, "Version:        1.0.23\n", pkg.config_file.read_text(), []) == \
        "Update the packaging\n"


def test_update_files(tmp_path, monkeypatch):
    d = make_package(tmp_path, "vsimd", "rust-vsimd", "0.8.0")
    for rel in (".fmf/version", "plans/rust-deps.fmf", "tests/rust-deps/main.fmf"):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("x")
    monkeypatch.setattr(rd, "spec_sources", lambda spec: [
        ("Source0", "https://crates.io/api/v1/crates/vsimd/0.8.0/download#/vsimd-0.8.0.crate"),
        ("Source10", "https://raw.githubusercontent.com/Nugine/simd/abc/LICENSE"),
        ("Patch0", "vsimd-fix-metadata.diff")])
    repo, lookaside = rd.update_files(rd.local_packages(tmp_path)["vsimd"])
    assert set(repo) == {"rust-vsimd.spec", "rust2rpm.toml", "vsimd-fix-metadata.diff", ".fmf/version",
                         "plans/rust-deps.fmf", "tests/rust-deps/main.fmf"}
    assert [f.name for f in lookaside] == ["vsimd-0.8.0.crate", "LICENSE"]


def test_prepare_update_refuses_when_dist_git_moved(tmp_path, monkeypatch):
    import subprocess
    def sh(*args, cwd):
        subprocess.run(args, cwd=cwd, check=True, capture_output=True)
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
    (d / rd.DIST_GIT_FILE).write_text(f'package = "rust-zmij"\nbranch = "rawhide"\ncommit = "{adopted}"\n')
    actions = []
    monkeypatch.setattr(rd, "action", actions.append)
    assert rd.prepare_update(rd.local_packages(root)["zmij"], top, "rawhide", False) is False
    assert "moved since 'adopt'" in actions[0] and "two" in actions[0]
    assert not (d / rd.UPDATE_STATE).exists()


def test_prepare_update_uses_the_adopted_branch(tmp_path, monkeypatch):
    import subprocess
    def sh(*args, cwd):
        return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
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
    (d / rd.DIST_GIT_FILE).write_text(f'package = "rust-zmij"\nbranch = "f45"\ncommit = "{adopted}"\n')
    actions = []
    monkeypatch.setattr(rd, "action", actions.append)
    monkeypatch.setattr(rd, "update_files", lambda pkg: ({}, []))
    pkg = rd.local_packages(root)["zmij"]
    # f45 is where it was adopted, and it did not move (rawhide did)
    assert rd.prepare_update(pkg, top, None, False) is True  # no files to change: nothing to commit
    assert actions == []
    assert not (d / rd.UPDATE_STATE).exists()
    assert sh("git", "rev-parse", "HEAD", cwd=top / "rust-zmij") == adopted


# ─── CLI wiring ──────────────────────────────────────────────────────────────

def run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["rust-deps", *argv])
    rd.main()


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
    monkeypatch.setattr(rd, "fedora_index", lambda refresh=False, chroot=None: rd.FedoraIndex({}))
    def boom(name):
        raise urllib.error.URLError("no route to host")
    monkeypatch.setattr(rd, "crate_versions", boom)
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "--root", str(tmp_path), "status")
    err = capsys.readouterr().err
    assert "network request failed" in err and "Traceback" not in err
    assert e.value.code == 1
