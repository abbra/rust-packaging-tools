#!/bin/bash
# install.sh — install the fedora-rust-packaging skill.
#
# Usage: install.sh [TARGET…] [--link] [--bin DIR] [--zip FILE] [--uninstall]
#
# Targets (default: --claude):
#   --claude           ~/.claude/skills (Claude Code, all projects)
#   --project DIR      DIR/.claude/skills (Claude Code, one project; OMP also
#                      loads project-level .claude/skills)
#   --agents           ~/.agents/skills (OMP/oh-my-pi's own user skills
#                      directory, also read by other agents)
#   --dest DIR         any skills directory: every tool that loads Agent Skills
#                      (a directory with SKILL.md) from a folder
#
# Options:
#   --link             symlink instead of copying (edits here take effect at once)
#   --bin DIR          also put the 'rust-deps' command into DIR (e.g. ~/.local/bin)
#   --zip FILE         build FILE.zip for tools that install skills from an
#                      uploaded archive (e.g. claude.ai) — no install target needed
#   --uninstall        remove the skill (and --bin link) from the given targets
#   -h, --help         show this help

set -euo pipefail

NAME=fedora-rust-packaging
HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/$NAME"

die() { echo "install.sh: $*" >&2; exit 1; }
usage() { sed -n '2,/^$/{s/^# \{0,1\}//;p}' "$0"; exit 0; }

targets=()
link=0
bin=""
zip=""
uninstall=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --claude)    targets+=("$HOME/.claude/skills"); shift ;;
        --agents)    targets+=("$HOME/.agents/skills"); shift ;;
        --project)   [[ $# -ge 2 ]] || die "--project needs a directory"
                     targets+=("$(realpath -m "$2")/.claude/skills"); shift 2 ;;
        --dest)      [[ $# -ge 2 ]] || die "--dest needs a directory"
                     targets+=("$(realpath -m "$2")"); shift 2 ;;
        --link)      link=1; shift ;;
        --bin)       [[ $# -ge 2 ]] || die "--bin needs a directory"
                     bin=$(realpath -m "$2"); shift 2 ;;
        --zip)       [[ $# -ge 2 ]] || die "--zip needs a file name"
                     zip=$(realpath -m "$2"); shift 2 ;;
        --uninstall) uninstall=1; shift ;;
        -h|--help)   usage ;;
        *)           die "unknown argument: $1 (see --help)" ;;
    esac
done
if [[ ${#targets[@]} -eq 0 && -z "$zip" && -z "$bin" ]]; then
    targets=("$HOME/.claude/skills")
fi

is_ours() {  # is $1 an installed copy/link of this skill?
    # a live copy or link must carry this skill's SKILL.md; only a dangling
    # link, which is what --link leaves when the checkout moves, counts as ours
    [[ -L "$1" && ! -e "$1/SKILL.md" ]] || grep -qs "^name: $NAME\$" "$1/SKILL.md"
}

# ── uninstall ────────────────────────────────────────────────────────────────
if [[ $uninstall == 1 ]]; then
    for t in "${targets[@]}"; do
        dest="$t/$NAME"
        if [[ -e "$dest" || -L "$dest" ]]; then
            is_ours "$dest" || die "$dest is not this skill; not removing it"
            rm -rf "$dest"
            echo "removed $dest"
        fi
    done
    if [[ -n "$bin" && -L "$bin/rust-deps" ]]; then
        [[ $(readlink "$bin/rust-deps") == */$NAME/scripts/rust-deps ]] \
            || die "$bin/rust-deps does not link to this skill; not removing it"
        rm -f "$bin/rust-deps"
        echo "removed $bin/rust-deps"
    fi
    exit 0
fi

# ── install ──────────────────────────────────────────────────────────────────
# sanity checks on the skill itself: only for the operations that read the tree
[[ -f "$SRC/SKILL.md" ]] || die "$SRC/SKILL.md not found"
grep -q "^name: $NAME\$" "$SRC/SKILL.md" || die "SKILL.md 'name:' must be $NAME (the directory name)"
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$SRC/scripts/rust-deps" \
    || die "scripts/rust-deps has a syntax error"
find "$SRC" -name __pycache__ -type d -prune -exec rm -rf {} +
chmod +x "$SRC/scripts/rust-deps"

first=""
for t in ${targets[@]+"${targets[@]}"}; do
    dest="$t/$NAME"
    mkdir -p "$t"
    if [[ -e "$dest" || -L "$dest" ]]; then
        is_ours "$dest" || die "$dest exists and is not this skill; not replacing it"
        rm -rf "$dest"
    fi
    if [[ $link == 1 ]]; then
        ln -s "$SRC" "$dest"
        echo "linked    $dest -> $SRC"
    else
        cp -a "$SRC" "$dest"
        echo "installed $dest"
    fi
    first=${first:-$dest}
done

if [[ -n "$bin" ]]; then
    mkdir -p "$bin"
    ln -sfn "${first:-$SRC}/scripts/rust-deps" "$bin/rust-deps"
    echo "linked    $bin/rust-deps -> ${first:-$SRC}/scripts/rust-deps"
    case ":$PATH:" in *":$bin:"*) ;; *) echo "note: $bin is not in \$PATH" ;; esac
fi

if [[ -n "$zip" ]]; then
    [[ "$zip" == *.zip ]] || zip="$zip.zip"
    if [[ -e "$zip" ]] && ! python3 -c 'import sys, zipfile; sys.exit(0 if zipfile.is_zipfile(sys.argv[1]) else 1)' "$zip"; then
        die "$zip exists and not a zip archive; not overwriting it"
    fi
    (cd "$HERE" && python3 -m zipfile -c "$zip" "$NAME")
    echo "packed    $zip"
fi
