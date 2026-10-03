"""rust-deps trial: run cargo tests the way %check would."""

from __future__ import annotations

from pathlib import Path
import os
import re
import subprocess
import tomllib

from . import archives
from . import config
from . import packages
from . import pkginit
from . import regen
from . import util

TRIAL_TIMEOUT = 900  # seconds per cargo invocation; see 'trial --timeout'
TRIAL_OFFLINE = True  # run tests without network, like mock; see 'trial --online'


def test_config(pkg: packages.LocalPackage) -> dict:
    return tomllib.loads(pkg.config_file.read_text()).get("tests", {})


# rust2rpm writes each [tests] skip filter unquoted into a %cargo_test line of
# %check: the shell splits it at spaces and reads ( ) < > | & ; $ * ? [ ] and
# quotes, and rpm expands %.  Only these characters survive as written.
SHELL_SAFE_SKIP = re.compile(r"[A-Za-z0-9_:./+=,@-]+")


def skip_names(skip) -> list[str]:
    """All skip filters of a [tests] skip value: a list, a per-target table, or a lone string."""
    if isinstance(skip, str):
        return [skip]
    if isinstance(skip, dict):
        return [n for v in skip.values() for n in skip_names(v)]
    return list(skip)


def unsafe_skips(cfg: dict) -> list[str]:
    """Skip filters of a [tests] table that would break the %check shell command."""
    return [
        n for n in skip_names(cfg.get("skip", [])) if not SHELL_SAFE_SKIP.fullmatch(n)
    ]


def unsafe_skips_message(crate: str, names: list[str]) -> str:
    return (
        f"{crate}: [tests] skip filters {', '.join(map(repr, names))} are not shell-safe: rust2rpm writes "
        "them unquoted into %check, where they break the %cargo_test command; use a part of the test "
        "name without spaces or shell characters (e.g. '::chain_call'), matched without skip-exact"
    )


def feature_args(pkg: packages.LocalPackage) -> list[str]:
    f = tomllib.loads(pkg.config_file.read_text()).get("features", {})
    args = []
    if f.get("enable-all"):
        args.append("--all-features")
    if f.get("enable"):
        args += ["--features", ",".join(f["enable"])]
    if f.get("disable-default"):
        args.append("--no-default-features")
    return args


def target_args(target: str) -> list[str]:
    if target == "lib":
        return ["--lib"]
    if target == "bins":
        return ["--bins"]
    if target == "doc":
        return ["--doc"]
    kind, _, name = target.partition(":")
    return [f"--{kind}", name]


def spec_invocations(pkg: packages.LocalPackage) -> list[tuple[str, list[str]]]:
    """(label, cargo test args) exactly like rust2rpm's %cargo_test calls."""
    cfg = test_config(pkg)
    runs = cfg.get("run", True)
    if runs is False:
        return []
    if runs is True:
        runs = [None]
    elif isinstance(runs, str):
        runs = [runs]
    skip, exact = cfg.get("skip", []), cfg.get("skip-exact", False)
    out = []
    for t in runs:
        s = skip.get(t, []) if isinstance(skip, dict) else skip_names(skip)
        e = exact if isinstance(exact, bool) else exact.get(t, False)
        harness = (["--exact"] if e and s else []) + [
            a for x in s for a in ("--skip", x)
        ]
        out.append(
            (
                t or "all",
                (target_args(t) if t else []) + (["--", *harness] if harness else []),
            )
        )
    return out


def crate_targets(src: Path) -> dict[str, str]:
    """rust2rpm run-target name -> source path, for lib/tests/bins."""
    toml = tomllib.loads((src / "Cargo.toml").read_text())
    pkg = toml.get("package", {})
    targets = {}
    if "lib" in toml or (src / "src/lib.rs").exists():
        targets["lib"] = toml.get("lib", {}).get("path", "src/lib.rs")
    for t in toml.get("test", []):
        targets[f"test:{t['name']}"] = t.get("path", f"tests/{t['name']}.rs")
    if pkg.get("autotests", True) is not False:
        for p in (
            sorted((src / "tests").glob("*.rs")) if (src / "tests").is_dir() else []
        ):
            targets.setdefault(f"test:{p.stem}", f"tests/{p.name}")
        for p in (
            sorted((src / "tests").glob("*/main.rs"))
            if (src / "tests").is_dir()
            else []
        ):
            targets.setdefault(
                f"test:{p.parent.name}", f"tests/{p.parent.name}/main.rs"
            )
    return targets


def _cargo_test(
    src: Path, args: list[str], log: Path, build_only: bool = False
) -> tuple[int, str]:
    env = dict(
        os.environ,
        CARGO_TARGET_DIR=str(config.CACHE_DIR / "target"),
        CARGO_TERM_COLOR="never",
    )
    cmd = ["cargo", "build"] if build_only else ["cargo", "test", "--no-fail-fast"]
    if TRIAL_OFFLINE:
        # mock builds have no network: run without it (dependencies were
        # fetched beforehand), so tests that need the network fail here too.
        # mock does bring the loopback interface up, so do the same: tests
        # that only talk to 127.0.0.1/::1 must pass here as they do in mock.
        cmd = [*OFFLINE_PREFIX, *cmd, "--offline"]
    try:
        res = util.run(
            [*cmd, *args], cwd=src, env=env, capture_output=True, timeout=TRIAL_TIMEOUT
        )
        rc, out = res.returncode, res.stdout + res.stderr
    except subprocess.TimeoutExpired as exc:
        rc = 124
        out = (
            (exc.stdout or b"").decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        out += f"\nerror: TIMEOUT after {TRIAL_TIMEOUT}s\n"
    with log.open("a") as f:
        f.write(f"\n$ {' '.join(cmd + args)}\n{out}")
    return rc, out


OFFLINE_PREFIX = ["unshare", "-rn", "sh", "-c", 'ip link set lo up && exec "$@"', "sh"]


def _summarize(out: str) -> str:
    passed = sum(int(m) for m in re.findall(r"test result: \w+\. (\d+) passed", out))
    failed = sum(int(m) for m in re.findall(r"; (\d+) failed", out))
    return f"{passed} passed, {failed} failed"


def trial(
    pkg: packages.LocalPackage,
    discover: bool,
    apply: bool = False,
    log_name: str | None = None,
) -> bool:
    """Build and test the patched crate with cargo (dependencies from crates.io).

    Without discover, run exactly the %cargo_test invocations of the spec.  With
    discover, try every test target and suggest a [tests] table.
    """
    if not discover and (bad := unsafe_skips(test_config(pkg))):
        # cargo would take them as they are, but %check fails on them
        util.info(f"== trial {pkg.crate} {pkg.version}")
        util.action(unsafe_skips_message(pkg.crate, bad))
        return False
    src = archives.extract_patched(pkg, config.CACHE_DIR / "trial")
    if TRIAL_OFFLINE:
        (src / "Cargo.lock").unlink(missing_ok=True)
        res = util.run(["cargo", "fetch"], cwd=src, capture_output=True)
        if res.returncode:
            util.die(f"{pkg.crate}: cargo fetch failed:\n{res.stderr}")
    else:
        (src / "Cargo.lock").unlink(missing_ok=True)
    log = config.CACHE_DIR / "trial" / (log_name or f"{pkg.crate}.log")
    log.write_text("")
    fargs = feature_args(pkg)
    util.info(f"== trial {pkg.crate} {pkg.version} (log: {log})")
    if not discover:
        invocations = spec_invocations(pkg)
        build_only = not invocations
        if not invocations:
            util.info(
                "   tests are disabled in rust2rpm.toml; checking that the crate builds"
            )
            invocations = [("build", [])]
        ok = True
        for label, args in invocations:
            rc, out = _cargo_test(src, fargs + args, log, build_only=build_only)
            status = "ok" if rc == 0 else "FAILED"
            ok &= rc == 0
            util.info(f"   {label:24} {status:7} {_summarize(out)}")
            if rc:
                for ln in dict.fromkeys(
                    re.findall(r"^(?:error(?:\[E\d+\])?: .*|---- .* ----)$", out, re.M)
                ):
                    util.info(f"      {ln}")
        return ok

    targets = crate_targets(src)
    toml = tomllib.loads((src / "Cargo.toml").read_text())
    feats = toml.get("features", {})
    if "--all-features" in fargs:
        enabled = set(feats) | {"default"}
    else:
        start = (
            set(fargs[fargs.index("--features") + 1].split(","))
            if "--features" in fargs
            else set()
        )
        if "--no-default-features" not in fargs:
            start.add("default")
        closure = archives.feature_closure(feats, start)
        enabled = closure[0] | closure[1]
    skipped = {}
    for t in toml.get("test", []):
        need = [f for f in t.get("required-features", []) if f not in enabled]
        if need and f"test:{t['name']}" in targets:
            skipped[f"test:{t['name']}"] = need
            del targets[f"test:{t['name']}"]
    for t, need in skipped.items():
        util.info(
            f"   {t:24} skipped needs feature(s) {', '.join(need)}; %cargo_test skips it too"
        )
    # 1. which targets compile at all
    rc, out = _cargo_test(
        src,
        fargs + ["--no-run", *(["--lib"] if "lib" in targets else []), "--tests"],
        log,
    )
    broken, errors = {}, []
    for ln in out.splitlines():
        if m := re.search(
            r'could not compile `[^`]+` \((lib test|test "([^"]+)")\)', ln
        ):
            key = "lib" if m.group(1) == "lib test" else f"test:{m.group(2)}"
            first = next(
                (e for e in errors if not e.startswith("error: aborting")), "see log"
            )
            broken[key] = f"does not compile: {first[:110]}"
            errors = []
        elif ln.startswith("error"):
            errors.append(ln)
    if "lib" not in broken and re.search(r"could not compile `[^`]+` \(lib\)", out):
        util.die(
            f"{pkg.crate}: the library itself does not build; fix the Cargo.toml edits first (see {log})"
        )
    # 2. run the rest target by target
    failing: dict[str, list[str]] = {}
    results = {}
    for t in [t for t in targets if t not in broken] + ["doc"]:
        rc, out = _cargo_test(src, fargs + target_args(t), log)
        results[t] = (rc, _summarize(out))
        passed = sum(
            int(m) for m in re.findall(r"test result: \w+\. (\d+) passed", out)
        )
        if rc:
            # a target where nothing passes is disabled as a whole
            failing[t] = (
                re.findall(r"^test (\S+) \.\.\. FAILED$", out, re.M)
                if passed and rc != 124
                else []
            )
            if rc == 124:
                results[t] = (rc, f"timed out after {TRIAL_TIMEOUT}s")
    for t in targets:
        if t in broken:
            util.info(f"   {t:24} BROKEN  {broken[t]}")
    for t, (rc, summary) in results.items():
        util.info(f"   {t:24} {'ok' if rc == 0 else 'FAILED':7} {summary}")
    if not broken and not failing:
        util.info("   all test targets pass; no [tests] restrictions needed")
        if apply:
            write_tests_table(pkg, None)
        return True

    # suggestion: drop broken targets; skip a few failing tests, drop the rest
    run_targets, skip, comments = [], {}, []
    for t in list(targets) + ["doc"]:
        if t in broken:
            comments.append(f"TODO: explain why {t} is disabled: {broken[t]}")
            continue
        names = failing.get(t)
        if names is None:
            run_targets.append(t)
        elif (
            names
            and len(names) <= 5
            and t != "doc"
            and all(SHELL_SAFE_SKIP.fullmatch(re.sub(r" - .*", "", n)) for n in names)
        ):
            run_targets.append(t)
            skip[t] = [re.sub(r" - .*", "", n) for n in names]
            comments.append(
                f"TODO: explain why these {t} tests are skipped: {', '.join(skip[t])}"
            )
        else:
            why = "takes too long" if results[t][0] == 124 else "fails at runtime"
            comments.append(f"TODO: explain why {t} is disabled ({why})")
    table = {"run": run_targets or False, "skip": skip, "comments": comments}
    util.info(
        "\nSuggested [tests] table for rust2rpm.toml (replace the TODO comments with real reasons):\n"
    )
    util.info(render_tests_table(table))
    if apply:
        write_tests_table(pkg, table)
        util.info(
            f"Written to {pkg.config_file}; regenerating the spec and re-running the trial."
        )
        util.action(
            f"{pkg.crate}: replace the TODO comments in rust2rpm.toml [tests] with the real reasons, then regen"
        )
        # separate log, so the discovery log that explains the failures survives
        return regen.regen(pkg, pkg.version) and trial(
            packages.local_packages(pkg.dir.parent)[pkg.crate],
            discover=False,
            log_name=f"{pkg.crate}.recheck.log",
        )
    return True


def render_tests_table(table: dict) -> str:
    out = ["[tests]"]
    run_ = table["run"]
    out.append(
        "run = false"
        if run_ is False
        else f"run = [{', '.join(pkginit.toml_str(r) for r in run_)}]"
    )
    if table["skip"]:
        for t, names in table["skip"].items():
            out.append(
                f"skip.{pkginit.toml_str(t) if ':' in t else t} = [{', '.join(pkginit.toml_str(n) for n in names)}]"
            )
        for t in table["skip"]:
            out.append(f"skip-exact.{pkginit.toml_str(t) if ':' in t else t} = true")
    out.append(f"comments = {pkginit.toml_list(table['comments'])}")
    return "\n".join(out) + "\n"


def write_tests_table(pkg: packages.LocalPackage, table: dict | None) -> None:
    text = pkg.config_file.read_text()
    text = re.sub(r"(?ms)^\[tests\]\n.*?(?=^\[|\Z)", "", text).rstrip("\n") + "\n"
    if table is not None:
        text += "\n" + render_tests_table(table)
    pkg.config_file.write_text(text)


def cmd_trial(args) -> None:
    global TRIAL_TIMEOUT
    TRIAL_TIMEOUT = args.timeout
    global TRIAL_OFFLINE
    TRIAL_OFFLINE = not args.online
    failed = [
        p.crate
        for p in packages.select(args.root, args.crates, args.all)
        if not trial(p, discover=args.discover, apply=args.apply)
    ]
    if failed:
        util.die(f"trial failed for: {', '.join(failed)}")
