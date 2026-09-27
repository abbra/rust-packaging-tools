"""Unit tests for rust-deps: offline, no rust2rpm runs, no network.

rust-deps is a script without a .py suffix; load it as a module.
"""

import importlib.machinery
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

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


# ─── COPR logs ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line, label", [
    ("Problem: nothing provides requested (crate(zmij/default) >= 1.0.23 with crate(zmij/default) < 2.0.0~)",
     "zmij >=1.0.23, <2.0.0 [default]"),
    (" Problem 1: nothing provides requested (crate(outref/default) >= 0.5.0 with crate(outref/default) < 0.6.0~)",
     "outref >=0.5.0, <0.6.0 [default]"),
    ("No matching package to install: 'pkgconfig(openssl) >= 3.0'", "pkgconfig(openssl) >= 3.0"),
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
