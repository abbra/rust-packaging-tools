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
    ├── scripts/rust-deps          the tool (Python 3, no extra modules beyond rust2rpm and python3-bugzilla)
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
./install.sh --bin ~/.local/bin       # also put 'rust-deps' on your PATH
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
# optional: mock, fedora-review (plus 'sudo usermod -aG mock $USER'), copr-cli
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
rust-deps copr --retry-failed --all --project username/repo   # resubmit what can succeed now
rust-deps resolve -r rhel+epel-10-x86_64 jsonschema     # check against another target
rust-deps review-request --all --project username/repo --fas username   # drafts; --file files them
rust-deps review-status --all                   # reviewer comments, next steps
rust-deps review-status --user username              # all your review tickets, tracked in ~/.cache
```

The full workflow and reference is in
[`fedora-rust-packaging/references/MANUAL.md`](fedora-rust-packaging/references/MANUAL.md).

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
