#!/usr/bin/env bash
# End-to-end run of rust-deps on a small crate from crates.io, as a packager
# would use it: needs network access (crates.io, Fedora mirrors) and the tools
# 'rust-deps doctor' checks.  Nothing is submitted anywhere: copr runs with -n.
#
#   tests/smoke.sh CRATE@VERSION      (default: num-cmp@0.1.0, which has no dependencies)
set -euo pipefail

spec=${1:-num-cmp@0.1.0}
if [[ $spec != *@* || -z ${spec%@*} || -z ${spec#*@} || $spec == *@*@* ]]; then
    echo "usage: tests/smoke.sh CRATE@VERSION (e.g. num-cmp@0.1.0)" >&2
    exit 2
fi
crate=${spec%@*}
version=${spec#*@}
here=$(cd "$(dirname "$0")/.." && pwd)
root=$(mktemp -d "${RUNNER_TEMP:-/tmp}/rust-deps-smoke.XXXXXX")
ws=$(mktemp -d "${RUNNER_TEMP:-/tmp}/rust-deps-ws.XXXXXX")
failed=1
cleanup() {
    if ((failed)); then
        echo "smoke test failed; the generated tree is kept in $root" >&2
    else
        rm -rf "$root" "$ws"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM
export XDG_CACHE_HOME="$root/.cache"   # a clean cache: no crates.io or dnf results from earlier runs
T=("$here/fedora-rust-packaging/scripts/rust-deps" --root "$root")

step() { printf '\n=== %s\n' "$*"; }

step doctor
"${T[@]}" doctor

step "resolve $crate"
"${T[@]}" resolve "$crate@=$version"

step "workspace (a two-crate workspace depending on $crate)"
cat > "$ws/Cargo.toml" <<EOF
[workspace]
resolver = "2"
members = ["crates/*"]
EOF
mkdir -p "$ws/crates/app/src" "$ws/crates/helper/src"
cat > "$ws/crates/app/Cargo.toml" <<EOF
[package]
name = "app"
version = "0.1.0"

[dependencies]
helper = { path = "../helper", version = "0.1.0" }
$crate = "=$version"
EOF
cat > "$ws/crates/helper/Cargo.toml" <<EOF
[package]
name = "helper"
version = "0.1.0"
EOF
echo 'fn main() {}' > "$ws/crates/app/src/main.rs"
echo 'pub fn f() {}' > "$ws/crates/helper/src/lib.rs"
"${T[@]}" workspace "$ws" | tee "$root/workspace.out"
grep -q "Workspace $ws (2 member crates" "$root/workspace.out"
grep -q 'WORKSPACE helper 0.1.0' "$root/workspace.out"
# the sibling crate is unpublished: it is never proposed for packaging
grep -qE '^(NEW|UPDATE|FEATURES)[[:space:]]+helper ' "$root/workspace.out" && exit 1
grep -qE "^(NEW|UPDATE|SYSTEM|PACKAGED|FEATURES)[[:space:]]+$crate " "$root/workspace.out" && exit 1

step "init $crate (test discovery ran)"
"${T[@]}" init --apply-tests "$crate@=$version" | tee "$root/init.out"
grep -qE 'Suggested \[tests\] table|all test targets pass' "$root/init.out"
test -f "$root/$crate/rust-$crate.spec"
test -f "$root/$crate/$crate-$version.crate"
spec_version=$(sed -n 's/^Version: *//p' "$root/$crate/rust-$crate.spec")
[[ $spec_version == "$version" ]] || { echo "spec Version is '$spec_version', expected '$version'" >&2; exit 1; }

step "regen $crate"
"${T[@]}" regen "$crate"

step "trial $crate (the spec's %cargo_test runs)"
grep -q '%cargo_test' "$root/$crate/rust-$crate.spec"
"${T[@]}" trial "$crate"

step "srpm $crate (sources, %prep, rpmlint)"
"${T[@]}" srpm "$crate"
compgen -G "$root/$crate/rust-$crate-$version-*.src.rpm" >/dev/null \
    || { echo "no SRPM built for $crate $version" >&2; exit 1; }

step order
"${T[@]}" order --all
"${T[@]}" order --all --json | python3 -c 'import json, sys; assert json.load(sys.stdin) == [[sys.argv[1]]]' "$crate"

step "check-targets (Fedora Rawhide repositories)"
"${T[@]}" check-targets --all -r fedora-rawhide-x86_64

step "copr, dry run"
"${T[@]}" copr --all -n --project example/project -r fedora-rawhide-x86_64 | tee "$root/copr.out"
grep -q 'copr-cli build --nowait -r fedora-rawhide-x86_64 example/project' "$root/copr.out"
grep -qF "rust-$crate-$version-" "$root/copr.out"

step status
"${T[@]}" status

failed=0
printf '\nsmoke test passed\n'
