# rust-packaging-tools

Source tree of the **fedora-rust-packaging** skill. It packages Rust crates for
Fedora with rust2rpm, including every dependency crate that Fedora is missing.
It is both:

- a command-line tool for people: `rust-deps`, in
  `fedora-rust-packaging/scripts/`
- an [Agent Skill](https://agentskills.io): the `fedora-rust-packaging/`
  directory with `SKILL.md`, which Claude and other skill-enabled agents can
  use

```
rust-packaging-tools/
├── install.sh                     installs the skill and/or the command
└── fedora-rust-packaging/         the skill (self-contained, location independent)
    ├── SKILL.md                   instructions for agents
    ├── scripts/rust-deps          the tool (Python 3, no extra modules beyond rust2rpm)
    └── references/MANUAL.md       the manual: workflow, file formats, fixes
```

Nothing refers to where the skill is installed or where your packages live.
Every command works on a *packages root* (one sub-directory per crate). The
root is `--root DIR`, else `$RUST_DEPS_ROOT`, else the current directory.

## Install

```
./install.sh                          # Claude Code, all projects: ~/.claude/skills
./install.sh --project ~/src/myproj   # Claude Code, one project: <dir>/.claude/skills
./install.sh --dest DIR               # any other agent's skills directory
./install.sh --zip fedora-rust-packaging.zip   # archive for uploading as a skill
./install.sh --bin ~/.local/bin       # also put 'rust-deps' on your PATH
./install.sh --link …                 # symlink instead of copy (for development)
./install.sh --uninstall [targets…]   # remove again
```

Targets can be combined. Re-running `install.sh` updates an existing
installation. It refuses to overwrite anything that is not this skill.

Then check the prerequisites:

```
rust-deps doctor        # or: ~/.claude/skills/fedora-rust-packaging/scripts/rust-deps doctor
```

## Use it yourself

```
export RUST_DEPS_ROOT=~/src/packages
rust-deps resolve jsonschema                   # what is missing in Fedora?
rust-deps init --recursive jsonschema          # create all missing packages
rust-deps trial --discover --apply jsonschema  # pick tests that can run
rust-deps srpm --all && rust-deps mock-chain --all -r fedora-45-x86_64
rust-deps review --all -r fedora-45-x86_64      # fedora-review, before submitting
rust-deps copr --all --project me/rust -r fedora-rawhide-x86_64 --wait
rust-deps review-request --all --project me/rust --fas me   # drafts; --file files them
rust-deps review-status --all                   # reviewer comments, next steps
rust-deps review-status --user me              # all your review tickets, tracked in ~/.cache
```

The full workflow and reference is in
[`fedora-rust-packaging/references/MANUAL.md`](fedora-rust-packaging/references/MANUAL.md).

## Use it through an agent

Once installed, ask for the result, for example: "package the Rust crates
jsonschema needs for Fedora in ~/src/packages". The agent follows `SKILL.md`:
it runs `rust-deps` with your packages root, and resolves or reports each
problem. It builds in mock when you are in the `mock` group. It hands decisions
that affect other Fedora packages back to you.
