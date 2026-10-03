"""rust-deps regen: regenerate the spec with rust2rpm."""

from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import tomllib

from . import archives
from . import config
from . import copr
from . import edits
from . import packages
from . import pkginit
from . import tmt
from . import trial
from . import util


def regen(
    pkg: packages.LocalPackage,
    version: str | None,
    local_crate: Path | None = None,
    compat: bool | None = None,
) -> bool:
    """Regenerate spec + patches with rust2rpm; returns False on failure.

    compat: True makes it a compat package, False a regular one, None keeps what it is.
    """
    compat = pkg.compat if compat is None else compat
    declared = edits.load_edits(pkg.edits_file)
    cfg = tomllib.loads(pkg.config_file.read_text())
    copr._BUILD_INFO.pop(
        (str(pkg.dir), pkg.version), None
    )  # the spec and Cargo.toml change here
    copr._BUILD_INFO.pop((str(pkg.dir), version), None)
    if local_crate:
        # rust2rpm --path does not store the crate; trial and srpm need it here
        if local_crate.resolve() != (pkg.dir / local_crate.name).resolve():
            shutil.copy2(local_crate, pkg.dir / local_crate.name)
        local_crate = pkg.dir / local_crate.name
    target = (
        ["--path", str(local_crate)]
        if local_crate
        else [f"{pkg.crate}@{version}" if version else pkg.crate]
    )
    env = dict(os.environ)
    env.pop("VISUAL", None)  # rust2rpm prefers $VISUAL over $EDITOR
    manual = pkg.dir / f"{pkg.crate}-fix-metadata.diff"
    if declared:
        manual.unlink(missing_ok=True)
        env["EDITOR"] = str(config.SELF)
        env[config.EDITOR_ENV] = str(pkg.edits_file)
        mode = ["-p"]
        if not cfg.get("package", {}).get("cargo-toml-patch-comments"):
            util.action(
                f"{pkg.crate}: {config.EDITS_FILE} has edits but rust2rpm.toml has no cargo-toml-patch-comments"
            )
    else:
        env["EDITOR"] = "false"
        mode = ["-r"]  # re-apply a hand-made <crate>-fix-metadata.diff if there is one
        if manual.exists() and not cfg.get("package", {}).get(
            "cargo-toml-patch-comments"
        ):
            util.action(
                f"{pkg.crate}: {manual.name} is applied, but {config.EDITS_FILE} is empty and rust2rpm.toml has "
                "no cargo-toml-patch-comments; if it is left from earlier edits, delete it and regen"
            )
    cmd = [
        "rust2rpm",
        *mode,
        "-s",
        "--no-existence-check",
        *(["--compat"] if compat else []),
        *target,
    ]
    util.info(f"   rust2rpm {' '.join(cmd[1:])}")
    res = util.run(cmd, cwd=pkg.dir, env=env, capture_output=True)
    out = res.stdout + res.stderr
    ok = res.returncode == 0
    if not ok:
        print(out, file=sys.stderr)
        if "No license files were detected" in out:
            util.action(
                f"{pkg.crate}: add license-files + [[package.extra-sources]] to rust2rpm.toml (see the manual)"
            )
        return False
    if "TODO" in pkg.config_file.read_text():
        util.action(f"{pkg.crate}: rust2rpm.toml still contains TODO comments")
    if bad := trial.unsafe_skips(trial.test_config(pkg)):
        util.action(trial.unsafe_skips_message(pkg.crate, bad))
    if "Summary" in out and "too long" in out:
        util.action(
            f"{pkg.crate}: generated Summary is too long; set package.summary in rust2rpm.toml and regen"
        )
    if declared and not manual.exists():
        util.warn(f"{pkg.crate}: {config.EDITS_FILE} produced no changes to Cargo.toml")
    comments = cfg.get("package", {}).get("cargo-toml-patch-comments", [])
    if (
        manual.exists()
        and "unexpected_cfgs" in manual.read_text()
        and not any("unexpected_cfgs" in c or "cfg value" in c for c in comments)
    ):
        util.action(
            f"{pkg.crate}: the patch declares the dropped features as expected cfg values; add to "
            f'cargo-toml-patch-comments in rust2rpm.toml: "{edits.CHECK_CFG_COMMENT}"'
        )
    specs = sorted(pkg.dir.glob("rust-*.spec"), key=lambda f: f.stat().st_mtime)
    for old in specs[
        :-1
    ]:  # e.g. the regular spec after --compat, or rust-foo0.1 after an update
        util.info(f"   removing {old.name} (now {specs[-1].name})")
        old.unlink()
        for srpm in pkg.dir.glob(f"{old.stem}-[0-9]*.src.rpm"):
            srpm.unlink()
        # mock-chain's local repository would still offer the old package to later builds
        for built in (config.CACHE_DIR / "mock-repo" / "results").glob(
            f"*/{old.stem}-[0-9]*"
        ):
            util.info(f"   removing {built} from the mock-chain repository")
            shutil.rmtree(built)
            if (built.parent / "repodata").exists() and shutil.which("createrepo_c"):
                util.run(["createrepo_c", "-q", "--update", str(built.parent)])
    new = packages.local_packages(pkg.dir.parent).get(pkg.crate)
    if new and new.version and LICENSE_FIXME_RE.search(new.spec.read_text()):
        if lic := binary_license(new):
            util.info(f"   License of the executables: {lic}")
            new.spec.write_text(
                LICENSE_FIXME_RE.sub(
                    lambda m: "# the crates linked into the executables, as %%cargo_license_summary reports them "
                    f"(filled in by rust-deps)\nLicense:{m.group(1)}{lic}",
                    new.spec.read_text(),
                )
            )
        else:
            util.action(
                f"{pkg.crate}: cannot compute the License of the executables (cargo fetch or cargo2rpm "
                "license-summary failed); see %cargo_license_summary in a mock build log"
            )
    if (
        new
        and new.version
        and (
            fixme := [
                ln.strip() for ln in new.spec.read_text().splitlines() if "FIXME" in ln
            ]
        )
    ):
        util.action(
            f"{pkg.crate}: the spec still has FIXME: {'; '.join(fixme)}. For a cdylib crate whose shared "
            "library is not shipped, set cargo-install-bin = false and suppress-cdylib-install-fixme = true "
            "under [package] in rust2rpm.toml"
        )
    if new and new.version:
        for old in pkg.dir.glob(f"{pkg.crate}-*.crate"):
            if old.name != f"{pkg.crate}-{new.version}.crate":
                old.unlink()
        requires = cfg.get("requires", {})
        if new.crate_file.exists() and not (
            requires.get("build") or requires.get("lib")
        ):
            if hint := pkginit.sys_hint(
                tomllib.loads(archives.read_crate_member(new.crate_file, "Cargo.toml"))
            ):
                util.action(f"{pkg.crate}: {hint}")
        if shebangs := foreign_shebangs(new.crate_file, cfg):
            listing = ", ".join(f"{f} (#!{i})" for f, i in shebangs)
            util.action(
                f"{pkg.crate}: executable files with an interpreter outside /usr/bin: {listing}; rpm makes "
                "-devel require that interpreter, which no package provides. Remove them or drop their "
                'executable bit in rust2rpm.toml [scripts.prep] (e.g. post = ["rm -r wasm/"]), with a '
                "comment saying why"
            )
        if compiled := compiled_doc_files(new.crate_file, new.spec):
            util.action(
                f"{pkg.crate}: {', '.join(compiled)} marked %doc but compiled into the crate (include_str!); "
                "the package must work without its documentation, so add to [package] in rust2rpm.toml: "
                f"doc-files.exclude = {json.dumps(compiled)}, with a comment saying why"
            )
        tmt.write_tmt(new)
    return True


LICENSE_FIXME_RE = re.compile(
    r"^# FIXME: paste output of %%cargo_license_summary here\nLicense:( +)# FIXME$",
    re.M,
)


def cargo_feature_flags(flags: list[str]) -> list[str]:
    """Translate rust2rpm's %cargo_build / %cargo_generate_buildrequires feature flags
    (-a, -n, -f FEAT, -fFEAT) into cargo / cargo2rpm arguments."""
    args, it = [], iter(flags)
    for f in it:
        if f in ("-a", "-n"):
            args.append({"-a": "--all-features", "-n": "--no-default-features"}[f])
        elif f.startswith("-f"):
            args += ["--features", f[2:] or next(it, "")]
    return args


def binary_license(pkg: packages.LocalPackage) -> str | None:
    """The License of the package's executables: what %cargo_license_summary reports.

    rust2rpm can only fill it in where the dependencies are installed; here
    cargo fetches them from crates.io, and cargo2rpm sums up the licenses of
    the crates linked into the binaries (no build, dev or proc-macro edges).
    """
    spec = pkg.spec.read_text()
    m = re.search(r"^%cargo_build(.*)$", spec, re.M)
    args = cargo_feature_flags((m.group(1) if m else "").split())
    # cargo tree as %cargo_license_summary (cargo2rpm) runs it, but online: offline, a fresh
    # resolution without dev-dependencies can miss crates that 'cargo fetch' did not index
    env = {**os.environ, "RUSTC_BOOTSTRAP": "1"}
    with tempfile.TemporaryDirectory() as tmp:
        src = archives.extract_patched(pkg, Path(tmp))
        res = util.run(
            [
                "cargo",
                "tree",
                "-Zavoid-dev-deps",
                "--workspace",
                "--edges=no-build,no-dev,no-proc-macro",
                "--target=all",
                "--prefix=none",
                "--format",
                "# {l}",
                *args,
            ],
            cwd=src,
            env=env,
            capture_output=True,
        )
    if res.returncode:
        return None
    licenses = set()
    for ln in res.stdout.splitlines():
        lic = ln.removeprefix("# ").removesuffix(" (*)").strip().replace("/", " OR ")
        if not lic:
            return None  # a crate with only a license-file: needs a look
        if (
            " OR " in lic and "(" not in lic and " AND " not in lic
        ):  # MIT OR Apache-2.0 == Apache-2.0 OR MIT
            lic = " OR ".join(sorted(lic.split(" OR ")))
        licenses.add(lic)
    parts = sorted(licenses, key=lambda x: (" " in x, x))
    return " AND ".join(f"({p})" if len(parts) > 1 and " " in p else p for p in parts)


def compiled_doc_files(crate_file: Path, spec: Path) -> list[str]:
    """%doc files of the spec that the crate's sources include with include_str!/include_bytes!."""
    docs = re.findall(r"^%doc %\{crate_instdir\}/(\S+)$", spec.read_text(), re.M)
    sources = "\n".join(
        archives.read_crate_member(crate_file, m) or ""
        for m in archives.crate_members(crate_file)
        if m.endswith(".rs") and not m.startswith(("tests/", "benches/", "examples/"))
    )
    return [
        d
        for d in docs
        if re.search(
            r'include_(?:str|bytes)!\s*\(\s*"(?:[^"]*/)?' + re.escape(d) + '"', sources
        )
    ]


SYSTEM_BIN_DIRS = ("/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/")


def foreign_shebangs(crate_file: Path, config: dict) -> list[tuple[str, str]]:
    """Executable files whose #! interpreter is outside the system's bin directories.

    rpm turns the interpreter of an executable file into a requirement of the
    package that ships it, so the -devel package would require, e.g.,
    /usr/local/bin/python, which no package provides.  Files that the prep
    scripts of rust2rpm.toml mention (removed, or made non-executable) count
    as handled.
    """
    scripts = config.get("scripts", {}).get("prep", {})
    handled = " ".join(scripts.get("pre", []) + scripts.get("post", []))
    words = {w.strip("/").rstrip("/") for w in handled.split() if not w.startswith("-")}
    found = []
    with tarfile.open(crate_file) as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.mode & 0o111:
                continue
            path = m.name.split("/", 1)[1] if "/" in m.name else m.name
            if any(w and (path == w or path.startswith(w + "/")) for w in words):
                continue
            first = tf.extractfile(m).readline(200).decode(errors="replace").strip()
            if not first.startswith("#!"):
                continue
            interpreter = first[2:].split()[0] if first[2:].split() else ""
            if interpreter and not interpreter.startswith(SYSTEM_BIN_DIRS):
                found.append((path, interpreter))
    return found


def cmd_regen(args) -> None:
    failed = []
    pkgs = packages.select(args.root, args.crates, args.all)
    if args.crate_file and len(pkgs) != 1:
        util.die("--crate-file needs exactly one crate")
    for pkg in pkgs:
        version = None if args.latest else (args.version or pkg.version)
        if args.crate_file:
            util.info(f"== {pkg.crate} from {args.crate_file}")
        else:
            util.info(f"== {pkg.crate} {version or '(latest)'}")
        if not regen(
            pkg,
            version,
            args.crate_file.resolve() if args.crate_file else None,
            args.compat,
        ):
            failed.append(pkg.crate)
    if failed:
        util.die(f"regen failed for: {', '.join(failed)}")
