# rust-packaging-tools

Set of tools to aid packaging Rust crates for Fedora Project.

This project provides the **fedora-rust-packaging** skill. It packages Rust
crates for Fedora with rust2rpm, including every dependency crate that Fedora
is missing.

The project consists of two components:

- a command-line tool for people: `rust-deps`, in `fedora-rust-packaging/scripts/`
- an [Agent Skill](https://agentskills.io): the `fedora-rust-packaging/`
  directory with `SKILL.md`, which Claude and other skill-enabled agents can
  use

```
rust-packaging-tools/
├── install.sh                     installs the skill and/or the command
└── fedora-rust-packaging/         the skill (self-contained, location independent)
    ├── SKILL.md                   instructions for agents
    ├── scripts/rust-deps          the tool (Python 3.12+, no extra modules beyond rust2rpm and
    │                              python3-bugzilla; 'tui' additionally uses python3-textual)
    └── references/MANUAL.md       the manual: workflow, file formats, fixes
```

Nothing refers to where the skill is installed or where your packages live.
Every command works on a *packages root* (one sub-directory per crate). The
root is `--root DIR`, else `$RUST_DEPS_ROOT`, else the current directory.

## Install

```
./install.sh                          # Claude Code, all projects: ~/.claude/skills
./install.sh --project ~/src/myproj   # Claude Code, one project: <dir>/.claude/skills
./install.sh --agents                 # OMP (oh-my-pi) and others: ~/.agents/skills
./install.sh --dest DIR               # any other agent's skills directory
./install.sh --zip fedora-rust-packaging.zip   # archive for uploading as a skill
./install.sh --bin ~/.local/bin       # put 'rust-deps' on your PATH (with no other target:
                                      # nothing is installed and it links into this checkout)
./install.sh --link …                 # symlink instead of copy (for development)
./install.sh --uninstall [targets…]   # remove again
```

Targets can be combined. Re-running `install.sh` updates an existing
installation. It refuses to overwrite anything that is not this skill.

After installation, check the prerequisites:

```
rust-deps doctor        # or: ~/.claude/skills/fedora-rust-packaging/scripts/rust-deps doctor
$ ./fedora-rust-packaging/scripts/rust-deps --root ~/src/packages doctor
   ok      rust2rpm   /usr/sbin/rust2rpm
   ok      cargo      /usr/sbin/cargo
   ok      rpmbuild   /usr/sbin/rpmbuild
   ok      spectool   /usr/sbin/spectool
   ok      rpmlint    /usr/sbin/rpmlint
   ok      dnf        /usr/sbin/dnf
   ok      patch      /usr/sbin/patch
   ok      unshare    /usr/sbin/unshare
   ok      ip         /usr/sbin/ip
   ok      mock       /usr/sbin/mock
   ok      copr-cli   /usr/sbin/copr-cli
   ok      fedora-review /usr/sbin/fedora-review
   ok      mock group
   root    /home/username/src/packages (19 packages)
```

Using [toolbx](https://containertoolbx.org/install/) is recommended as it
allows to have easy environments for each Fedora release.

Expected packages to be installed:
```
sudo dnf install python3 rust2rpm cargo rpm-build rpmdevtools rpmlint dnf5 patch util-linux iproute
# optional: mock, fedora-review (plus 'sudo usermod -aG mock $USER'), copr-cli, python3-textual (for 'tui')
```

`rust2rpm` provides the `python3-cargo2rpm` module that `rust-deps` uses for
semver matching. `rust-deps doctor` checks all of this, including membership
in the `mock` group and the user namespaces (with a working loopback
interface) that offline trials need.

## Use it yourself

```
export RUST_DEPS_ROOT=~/src/packages
rust-deps resolve jsonschema                   # what is missing in Fedora?
rust-deps init --recursive jsonschema          # create all missing packages
rust-deps trial --discover --apply jsonschema  # pick tests that can run
rust-deps srpm --all && rust-deps mock-chain --all -r fedora-45-x86_64
rust-deps review --all -r fedora-45-x86_64      # fedora-review, before submitting
rust-deps check-targets --all --project username/repo   # will every chroot find its BuildRequires?
rust-deps copr --all --project username/repo -r fedora-rawhide-x86_64 --wait
rust-deps copr-status --all --project username/repo   # why builds failed, per chroot
rust-deps copr-log jsonschema -r fedora-44-x86_64 --project username/repo   # errors, explained warnings
rust-deps copr --retry-failed --all --project username/repo   # resubmit what can succeed now
rust-deps resolve -r rhel+epel-10-x86_64 jsonschema     # check against another target
rust-deps tmt --all --project username/repo -r fedora-45-x86_64   # the generated Fedora CI tests
rust-deps review-plan --all --project username/repo   # what to submit, in which order, which drafts are ready
rust-deps dist-git --dir ~/src/fedora zmij   # an update of a Fedora package, committed in its dist-git checkout
rust-deps review-request --all --project username/repo --fas username   # drafts; --file files them
rust-deps review-status --all                   # reviewer comments, next steps
rust-deps status --project username/repo        # packaged vs. crates.io vs. each Fedora/EPEL release
rust-deps update native-ossl-sys native-ossl --version 0.3.1   # to a new upstream release, in build order
rust-deps review-status --user username              # all your review tickets, tracked in ~/.cache
rust-deps tui                                        # all of the above in a text UI (python3-textual)
```

`rust-deps tui` opens the whole tool in a terminal UI (built with
[Textual](https://github.com/Textualize/textual)): a sidebar lists every
command in workflow order, and it opens on an **Overview**: every package
under the root with its stage on the packaging lifecycle graph (spec → tests
→ SRPM → review → draft → filed, with the adopted and target-limited
branches), and the next step the graph implies for each — selecting one jumps
to that command with the right crates already picked. The Overview refreshes
after every run.
The form for the selected command is built from the same argparse metadata as
`--help` (so the UI and the command line can never drift apart), rendered as
what the parser says it is, in two panes side by side: the crate selection is
a list of checkboxes over the packages under `--root` plus a field for new
`CRATE[@REQ]` names on the left, every option a readable row of its own on the
right (flags with a short form of their `--help` inline), so a long crate list
never hides the options;
mutually exclusive options (`regen --latest/--version/--crate-file`,
`--compat/--no-compat`) are one mode choice, repeatable options (`-r`,
`--manifest`, `--koji-task`, …) are add/remove rows, and required arguments
gate the Run button. The exact `rust-deps …` command line is shown live as
you fill the form. Output streams into the view — diagnostics colored,
`status`, `doctor`, `trial`, `srpm` and any `--json` result rendered as
tables, and a run whose output has no table shape stays in the Log view.
Results feed back into the UI: a `trial` run becomes a verdict table (crate,
target, status, summary, first error) with next steps under it, following the
discovery workflow: plain failures go to `--discover`, a suggested `[tests]`
table goes to `--discover --apply` (write, regenerate, recheck), a passing
recheck goes to `regen` after the TODO reasons are filled in, and passing
crates go to `srpm` — selecting one reopens that command with the crates
already picked and the suggested flags set. The same applies to what any
other command proposes in its own output: review-plan's `run 'review' first`
on a NOT READY package, review-status' `next:` lines, and copr-status'
resubmit command all become jumps with the right crates picked. Keys:
arrows/enter pick a command or a suggested next step, `tab` moves through the
form, `ctrl+r` runs, `escape` cancels, `ctrl+q` quits.

The full workflow and reference is in
[`fedora-rust-packaging/references/MANUAL.md`](fedora-rust-packaging/references/MANUAL.md).

## Tests

```
python3 -m pytest tests          # unit tests: offline, need python3-pytest, python3-pyyaml
                                 # and rust2rpm (for its cargo2rpm module)
tests/smoke.sh [crate@version]   # end to end on a crate from crates.io (default num-cmp@0.1.0):
                                 # doctor, resolve, init, regen, trial, srpm, order,
                                 # check-targets, copr -n, status; needs network
```

GitHub Actions runs both in a Fedora container on every push and pull request
(`.github/workflows/test.yml`); the smoke test's container is privileged, as
`trial` runs tests offline in a user namespace.

## Use it through an agent

Once installed, ask for the result, for example: "package the Rust crates
jsonschema needs for Fedora in ~/src/packages". The agent follows `SKILL.md`:
it runs `rust-deps` with your packages root, and resolves or reports each
problem. It builds in mock when you are in the `mock` group. It hands decisions
that affect other Fedora packages back to you.

To name the task directly, run the skill as a command with arguments:

| harness | command |
|---|---|
| Claude Code | `/fedora-rust-packaging jsonschema --root ~/src/packages` |
| OMP (oh-my-pi) | `/skill:fedora-rust-packaging jsonschema --root ~/src/packages` |

The arguments are crate names, `--manifest <Cargo.toml>`, or a single
`rust-deps` command such as `review-status --user <login>`; with none, the
agent asks.

### OMP (oh-my-pi)

OMP loads user skills from `~/.agents/skills` (`./install.sh --agents`).
It ignores `~/.claude/skills` unless `skills.enableClaudeUser` is turned on
(`omp config set skills.enableClaudeUser true`), but it does load
project-level `.claude/skills`. Check that it sees the skill with
`omp read skill://fedora-rust-packaging`.
