#!/usr/bin/bash
# End-to-end run of rust-deps on a small crate from crates.io, as a packager
# would use it: needs network access (crates.io, Fedora mirrors) and the tools
# 'rust-deps doctor' checks.  Nothing is submitted anywhere: copr runs with -n.
#
#   tests/smoke.sh [CRATE@VERSION]      (default: num-cmp@0.1.0, which has no dependencies)
set -euo pipefail

spec=${1:-num-cmp@0.1.0}
crate=${spec%@*}
version=${spec#*@}
here=$(cd "$(dirname "$0")/.." && pwd)
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
export XDG_CACHE_HOME="$root/.cache"   # a clean cache: no crates.io or dnf results from earlier runs
T=("$here/fedora-rust-packaging/scripts/rust-deps" --root "$root")

step() { printf '\n=== %s\n' "$*"; }

step doctor
"${T[@]}" doctor

step "resolve $crate"
"${T[@]}" resolve "$crate@=$version"

step "init $crate (test discovery, table applied)"
"${T[@]}" init --apply-tests "$crate@=$version"
test -f "$root/$crate/rust-$crate.spec"
test -f "$root/$crate/$crate-$version.crate"
grep -q "^Version: *$version$" "$root/$crate/rust-$crate.spec"

step "regen $crate"
"${T[@]}" regen "$crate"

step "trial $crate (the spec's %cargo_test runs)"
"${T[@]}" trial "$crate"

step "srpm $crate (sources, %prep, rpmlint)"
"${T[@]}" srpm "$crate"
ls "$root/$crate"/rust-"$crate"-"$version"-*.src.rpm

step order
"${T[@]}" order --all
"${T[@]}" order --all --json | python3 -c "import json, sys; assert json.load(sys.stdin) == [['$crate']]"

step "check-targets (Fedora Rawhide repositories)"
"${T[@]}" check-targets --all -r fedora-rawhide-x86_64

step "copr, dry run"
"${T[@]}" copr --all -n --project example/project -r fedora-rawhide-x86_64 | tee "$root/copr.out"
grep -q "copr-cli build --nowait -r fedora-rawhide-x86_64 example/project .*rust-$crate-$version-" "$root/copr.out"

step status
"${T[@]}" status

printf '\nsmoke test passed\n'
