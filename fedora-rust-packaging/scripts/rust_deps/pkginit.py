"""rust-deps init: create a package directory for a new crate."""

from __future__ import annotations

from pathlib import Path
import functools
import json
import re
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from . import archives
from . import config
from . import cratesio
from . import edits
from . import fedora
from . import packages
from . import regen
from . import resolver
from . import trial
from . import util
from . import versions


def suggest_edits(
    toml: dict, index: fedora.FedoraIndex, local: dict[str, packages.LocalPackage]
) -> tuple[dict, list[str]]:
    """Suggest cargo-toml edits for deps that are not packaged in Fedora."""

    def available(d):
        dn = d["crate_id"]
        if dn in local and local[dn].version:
            return versions.req_matches(d["req"], local[dn].version)
        return index.best(dn, d["req"]) is not None

    edits: dict = {}
    notes: list[str] = []
    features = toml.get("features", {})
    deps = [
        d for d in archives.manifest_deps(toml) if not versions.is_foreign(d["target"])
    ]

    drop_dev = sorted(
        {d["name"] for d in deps if d["kind"] == "dev" and not available(d)}
    )
    for d in deps:
        if (
            d["kind"] == "dev"
            and d["name"] in drop_dev
            and index.versions(d["crate_id"])
        ):
            notes.append(
                f"dev-dependency {d['crate_id']} {d['req']}: Fedora has {', '.join(index.versions(d['crate_id']))}"
                f" — instead of dropping it, you may try [set-dev-version] {d['name']} = \"<fedora version>\""
            )
    # dev-dependencies that Fedora has, but without the requested features
    # (e.g. getrandom with "wasm_js", only needed for wasm test builds)
    for d in deps:
        if d["kind"] != "dev" or d["name"] in drop_dev or d["crate_id"] in local:
            continue
        fv = index.best(d["crate_id"], d["req"])
        miss = fv and index.missing_features(d["crate_id"], fv, d["features"])
        if miss:
            drop_dev.append(d["name"])
            notes.append(
                f"dev-dependency {d['crate_id']} {fv} lacks features {miss} in Fedora; it is dropped — "
                "check that the tests do not use it, otherwise disable the tests that do"
            )
    drop_dev = sorted(set(drop_dev))
    drop_dep = sorted(
        {
            d["name"]
            for d in deps
            if d["kind"] != "dev" and d["optional"] and not available(d)
        }
    )
    required_missing = sorted(
        {
            f"{d['crate_id']} {d['req']}"
            for d in deps
            if d["kind"] != "dev" and not d["optional"] and not available(d)
        }
    )

    # features that need a feature Fedora's package of a dependency does not have
    by_name = {d["name"]: d for d in deps if d["kind"] != "dev"}
    drop_feat: set[str] = set()

    def missing_dep_feature(name: str, feat: str) -> bool:
        d = by_name.get(name)
        if d is None or d["crate_id"] in local:
            return False
        fv = index.best(d["crate_id"], d["req"])
        return bool(fv and index.missing_features(d["crate_id"], fv, [feat]))

    for f, items in features.items():
        for it in items:
            if "/" in it:
                dep, feat = it.split("/", 1)
                if f != "default" and missing_dep_feature(dep.rstrip("?"), feat):
                    drop_feat.add(f)
                    notes.append(
                        f"feature '{f}' needs {it}, which Fedora's package does not provide"
                    )
    for d in deps:
        if d["kind"] != "dev" and not d["optional"] and d["crate_id"] not in local:
            fv = index.best(d["crate_id"], d["req"])
            miss = fv and index.missing_features(d["crate_id"], fv, d["features"])
            if miss:
                notes.append(
                    f"required dependency {d['crate_id']} {fv} lacks features {miss} in Fedora: "
                    "patch the feature list (set-version will not help) or update that package"
                )

    # every feature that (transitively) enables a dropped dependency must go
    changed = True
    while changed:
        changed = False
        for f, items in features.items():
            if f in drop_feat or f == "default":
                continue
            for it in items:
                base = it.removeprefix("dep:").split("/")[0].rstrip("?")
                hard = not it.split("/")[0].endswith("?")
                if (
                    base in drop_dep
                    and (it.startswith("dep:") or "/" not in it or hard)
                ) or base in drop_feat:
                    drop_feat.add(f)
                    changed = True
                    break
    default_hit = [
        it
        for it in features.get("default", [])
        if it.removeprefix("dep:").split("/")[0].rstrip("?")
        in drop_feat | set(drop_dep)
    ]
    if default_hit:
        notes.append(
            f"default features {default_hit} need unpackaged crates and are removed from 'default'; "
            "decide whether another feature should be enabled instead (add-default-features)"
        )

    if drop_dev:
        edits["drop-dev-dependencies"] = drop_dev
    if drop_dep:
        edits["drop-dependencies"] = drop_dep
    if drop_feat:
        edits["drop-features"] = sorted(drop_feat)
    if required_missing:
        notes.append(
            "required dependencies are not packaged yet (package them first, or use --recursive): "
            + ", ".join(required_missing)
        )
    return edits, notes


def ships_license(crate_file: Path) -> bool:
    return any(
        config.LICENSE_RE.match(Path(m).name)
        for m in archives.crate_members(crate_file)
        if "/" not in m
    )


def vcs_commit(crate_file: Path) -> str | None:
    """The upstream git commit a crate was published from (.cargo_vcs_info.json)."""
    return (
        json.loads(
            archives.read_crate_member(crate_file, ".cargo_vcs_info.json") or "{}"
        )
        .get("git", {})
        .get("sha1")
    )


def license_sources(
    crate_file: Path, toml: dict
) -> tuple[list[str], list[str], list[str]]:
    """(license-files, source URLs, notes) for crates that do not ship license texts."""
    if ships_license(crate_file):
        return [], [], []
    vcs = json.loads(
        archives.read_crate_member(crate_file, ".cargo_vcs_info.json") or "{}"
    )
    sha = vcs.get("git", {}).get("sha1")
    sub = vcs.get("path_in_vcs", "")
    meta = toml.get("package", {})
    repos = []
    for key in ("repository", "homepage"):
        r = (meta.get(key) or "").rstrip("/").removesuffix(".git")
        if re.match(r"https?://[^/]+/[^/]+/[^/]+$", r) and r not in repos:
            repos.append(r)
    if not sha or not repos:
        return (
            [],
            [],
            [
                "no license file in the crate and no VCS info to fetch one: add license-files/extra-sources by hand"
            ],
        )
    candidates = [
        "LICENSE",
        "LICENSE-MIT",
        "LICENSE-APACHE",
        "LICENSE.md",
        "LICENSE.txt",
        "COPYING",
        "UNLICENSE",
    ]
    for repo in repos:
        for raw in raw_url_templates(repo, sha):
            names, urls = [], []
            for prefix in ([sub] if sub else []) + [""]:
                for c in candidates:
                    url = raw.format(f"{prefix}/{c}" if prefix else c)
                    if url_exists(url) and c not in names:
                        names.append(c)
                        urls.append(url)
                if names:
                    return names, urls, []
    tried = ", ".join(repos)
    if not any(url_exists(r) for r in repos):
        return (
            [],
            [],
            [
                f"no license file in the crate, and {tried} is unreachable: find the real upstream "
                f"repository and add license-files/extra-sources by hand"
            ],
        )
    return (
        [],
        [],
        [
            f"no license file in the crate, and none found at {tried} for commit {sha}: the "
            "repository/homepage URL may be stale (a mirror, or the project moved) — find the "
            "repository that has this commit and add license-files/extra-sources by hand"
        ],
    )


def url_exists(url: str) -> bool:
    try:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": config.USER_AGENT}
        )
        urllib.request.urlopen(req, timeout=30)
        return True
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def raw_url_templates(repo: str, sha: str) -> list[str]:
    """Raw-file URL templates ('{}' = path in the repository) for a forge URL."""
    m = re.match(r"(https?://([^/]+))/([^/]+/[^/]+)$", repo)
    base, host, path = m.groups()
    if host == "github.com":
        return [f"https://raw.githubusercontent.com/{path}/{sha}/{{}}"]
    if host == "gitlab.com":
        return [f"{base}/{path}/-/raw/{sha}/{{}}"]
    # Forgejo/Gitea (codeberg.org, forge.fedoraproject.org, ...) or a self-hosted GitLab
    return [f"{base}/{path}/raw/commit/{sha}/{{}}", f"{base}/{path}/-/raw/{sha}/{{}}"]


SYS_BUILD_DEPS = (
    "pkg-config",
    "pkgconf",
    "system-deps",
    "bindgen",
    "cc",
    "cmake",
    "autotools",
    "vcpkg",
)


@functools.cache
def crate_links(name: str, req: str) -> str | None:
    """The `links` key of the newest release of a crate matching req (from the crates.io index)."""
    v = cratesio.pick_version(name, req)
    if not v:
        return None
    key = name.lower()
    path = {1: f"1/{key}", 2: f"2/{key}", 3: f"3/{key[0]}/{key}"}.get(
        len(key), f"{key[:2]}/{key[2:4]}/{key}"
    )
    cache = config.CACHE_DIR / "cratesio" / f"{name}-index.jsonl"
    if not cache.exists() or time.time() - cache.stat().st_mtime > 86400:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(cratesio._http_get(f"https://index.crates.io/{path}"))
        except urllib.error.URLError:
            return None
    for ln in cache.read_text().splitlines():
        entry = json.loads(ln)
        if entry["vers"] == v["num"]:
            return entry.get("links")
    return None


def sys_hint(toml: dict) -> str | None:
    """A reason to add system Build/Requires, for crates that link C libraries."""
    links = toml.get("package", {}).get("links")
    if links:
        # A required dependency that links the same library brings its system package: pyo3
        # (links = "pyo3-python") gets libpython through pyo3-ffi (links = "python"), whose
        # -devel requires python3-devel.  Only the same library counts: a dependency linking
        # another one (a libgit2 crate depending on libz-sys) says nothing about this one.
        parts = set(re.split(r"[-_]", links))
        for d in archives.manifest_deps(toml):
            if (
                d["kind"] == "normal"
                and not d["optional"]
                and (dl := crate_links(d["crate_id"], d["req"]))
                and (dl == links or dl in parts)
            ):
                links = None
                break
    bdeps = sorted(d for d in toml.get("build-dependencies", {}) if d in SYS_BUILD_DEPS)
    if not links and not bdeps:
        return None
    why = ", ".join(
        ([f"links = {links!r}"] if links else [])
        + ([f"build-dependencies {bdeps}"] if bdeps else [])
    )
    return (
        f"{why}: build.rs probably needs a system library; add it to rust2rpm.toml [requires] build "
        "(and lib, since build.rs also runs when dependent crates are built), e.g. "
        '"pkgconfig(openssl) >= 3.0"; read build.rs for the exact library and version'
    )


def toml_str(s: str) -> str:
    return json.dumps(s)


def toml_list(items: list[str], indent: str = "    ") -> str:
    if not items:
        return "[]"
    return "[\n" + "".join(f"{indent}{toml_str(i)},\n" for i in items) + "]"


def write_edits_file(path: Path, edits: dict, notes: list[str]) -> None:
    lines = [
        "# Declarative edits of the crate's Cargo.toml, applied by rust-deps when",
        "# regenerating <crate>-fix-metadata.diff.  Keep cargo-toml-patch-comments in",
        "# rust2rpm.toml in sync with these.  See 'rust-deps --help'.",
    ]
    lines += [f"# NOTE: {n}" for n in notes]
    lines.append("")
    tables = {}
    for k, v in edits.items():
        if isinstance(v, dict):
            tables[k] = v
        else:
            lines.append(f"{k} = {toml_list(v)}")
    for k, v in tables.items():
        lines.append(f"\n[{k}]")
        lines += [f"{toml_str(n)} = {toml_str(r)}" for n, r in v.items()]
    path.write_text("\n".join(lines) + "\n")


def write_config(
    path: Path, crate: str, declared: dict, lic_names: list[str], lic_urls: list[str]
) -> None:
    out = [f"# rust2rpm(1) configuration for rust-{crate}", ""]
    pkg = []
    if comments := edits.describe_edits(declared):
        pkg.append(f"cargo-toml-patch-comments = {toml_list(comments)}")
    if lic_names:
        pkg.append(f"license-files = {toml_list(lic_names)}")
    if pkg:
        out += ["[package]", *pkg, ""]
    for i, url in enumerate(lic_urls):
        out += [
            "[[package.extra-sources]]",
            f"number = {10 + i}",
            f"file = {toml_str(url)}",
            'comments = ["license text from upstream git (same commit as the crate); not included in the published crate"]',
            "",
        ]
    if lic_urls:
        cps = ", ".join(
            toml_str(f"cp -pav %{{SOURCE{10 + i}}} .") for i in range(len(lic_urls))
        )
        out += ["[scripts.prep]", f"post = [{cps}]", ""]
    path.write_text("\n".join(out).rstrip("\n") + "\n")


def add_target(pkg_dir: Path, chroot: str) -> None:
    f = pkg_dir / config.TARGETS_FILE
    only = tomllib.loads(f.read_text()).get("only", []) if f.exists() else []
    rel = fedora.chroot_release(chroot)
    if rel in only:
        return
    only.append(rel)
    f.write_text(
        "# Fedora ships this crate; the package is only for the targets that lack it.\n"
        "# 'copr' builds it only in their chroots.\n"
        f"only = [{', '.join(json.dumps(t) for t in only)}]\n"
    )
    util.info(
        f"   {pkg_dir.name}: built only for {', '.join(only)} (host Fedora has it; see {config.TARGETS_FILE})"
    )


def cmd_init(args) -> None:
    index = fedora.fedora_index(args.refresh, args.target)
    host = fedora.fedora_index(args.refresh) if args.target else None
    root: Path = args.root
    queue = []
    for spec in args.crates:
        name, _, req = spec.partition("@")
        queue.append((name, req or "*"))
    if args.recursive:
        needed = resolver.resolve(
            [(n, r, "(command line)", ["default"]) for n, r in queue],
            index,
            packages.local_packages(root),
        )
        queue = [
            (n.crate, f"={n.version}")
            for n in needed.values()
            if n.status in ("new", "update")
        ]
        util.info("Packages to create: " + ", ".join(f"{c} {r[1:]}" for c, r in queue))
    planned = {
        n: packages.LocalPackage(n, root / n, r.lstrip("="))
        for n, r in queue
        if r.startswith("=")
    }
    created, failed = [], []
    for name, req in queue:
        local = {**planned, **packages.local_packages(root)}
        d = root / name
        if (d / "rust2rpm.toml").exists() and not args.force:
            util.info(f"== {name}: already initialized in {d} (use --force to redo)")
            if host and (d / config.TARGETS_FILE).exists():
                add_target(d, args.target)  # needed by one more target
            continue
        v = cratesio.pick_version(name, req)
        if v is None:
            util.die(f"{name} {req}: not found on crates.io")
        version = v["num"]
        util.info(f"== {name} {version}")
        d.mkdir(parents=True, exist_ok=True)
        crate_file = cratesio.download_crate(name, version, d)
        toml = tomllib.loads(archives.read_crate_member(crate_file, "Cargo.toml"))
        declared, notes = suggest_edits(toml, index, local)
        lic_names, lic_urls, lic_notes = license_sources(crate_file, toml)
        notes += lic_notes
        if declared or not d.joinpath(config.EDITS_FILE).exists():
            write_edits_file(d / config.EDITS_FILE, declared, notes)
        write_config(d / "rust2rpm.toml", name, declared, lic_names, lic_urls)
        for n in notes:
            util.action(f"{name}: {n}")
        if host and host.best(name, f"={version}"):
            add_target(d, args.target)
        pkg = packages.LocalPackage(name, d, None)
        if regen.regen(pkg, version, compat=args.compat):
            created.append(pkg)
        else:
            failed.append(name)
    if created and not args.no_trial:
        for pkg in created:
            pkg = packages.local_packages(root)[pkg.crate]
            trial.trial(pkg, discover=True, apply=args.apply_tests)
    if failed:
        util.warn(
            "no spec was generated (and no trial run) for: "
            + ", ".join(failed)
            + "; resolve their ACTION NEEDED lines, then 'regen' and 'trial --discover'"
        )
