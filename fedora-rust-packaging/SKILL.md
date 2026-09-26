---
name: fedora-rust-packaging
description: Package Rust crates for Fedora with rust2rpm, including every dependency crate missing from Fedora. Use when asked to package a crate (or a project's missing Rust dependencies) as RPMs, to create or update rust-<crate> specs, to fix a rust2rpm Cargo.toml patch or test selection, or to build such packages in mock or COPR in dependency order.
compatibility: Fedora (or a Fedora-like system with dnf5) with python3, rust2rpm, cargo, rpm-build, rpmdevtools, rpmlint, patch, util-linux and iproute; network access to crates.io. mock, fedora-review and copr-cli are optional, for builds and reviews.
argument-hint: "<crate>[@ver]… | --manifest <Cargo.toml> | <rust-deps command> [args] [--root <dir>]"
metadata:
  version: "1.8"
---

# Fedora Rust crate packaging

All work goes through `scripts/rust-deps`, a CLI in this skill's directory; run
it with its full path. `references/MANUAL.md` is the complete manual. Read it
before the first use, for the file formats and the table of fixes for each
`ACTION NEEDED` message.

The skill directory comes from the harness:

- **Claude Code:** the "Base directory for this skill" line above this text.
- **OMP (oh-my-pi):** the `[Skill directory: …]` line after this text when the
  user ran `/skill:fedora-rust-packaging`. Otherwise use the `skill://` URL:
  OMP's bash tool runs `skill://fedora-rust-packaging/scripts/rust-deps` as
  that file, and its read tool reads
  `skill://fedora-rust-packaging/references/MANUAL.md`.
- **Elsewhere:** the directory that holds this `SKILL.md`.

## Invocation

Arguments given with the skill command (`/fedora-rust-packaging` in Claude
Code, `/skill:fedora-rust-packaging` in OMP): `$ARGUMENTS`

Claude Code puts the arguments in place of the placeholder above. OMP leaves
the placeholder as it is and appends the arguments after the skill text as
`User: <arguments>`. If neither shows any, there are none.

Read them as follows (forms can be combined with `--root <dir>`):

- **crate names**, optionally `name@version` (`jsonschema serde_json_path@0.7`):
  package them and every missing dependency (the whole workflow below).
- **`--manifest <path/Cargo.toml>`**, or a path to a project directory or its
  `Cargo.toml`: package the project's dependencies that Fedora is missing.
- **a `rust-deps` command** with its arguments (`status`, `resolve …`,
  `trial <crate>`, `srpm --all`, `review --all -r <chroot>`,
  `review-status --all`, `review-status --user <login>`, …): run only that
  step and report its result.
- **`--root <dir>`**: the packages root (see Setup).
- **nothing**: show the user these forms with one example each, and ask what
  to package and where the packages root is.

## Setup

1. **Packages root.** Each package lives in `<root>/<crate>/`. Use the directory
   the user names. If they name none, ask, and suggest an existing tree of
   such packages or a new one (e.g. `~/src/packages`). Never guess, and never
   use the skill's own directory. Pass it on every call:

   ```
   T="<skill dir>/scripts/rust-deps --root <packages root>"
   ```

   Setting `RUST_DEPS_ROOT=<packages root>` is equivalent.
2. **Prerequisites.** Run `$T doctor`. Install what it reports missing (ask
   before running `sudo`), or tell the user. If the user was only just added to
   the `mock` group, the current session may not have it yet: run mock
   commands through `sg mock -c '…'`.

## Rules

- **Never edit generated files:** `rust-<crate>.spec`, `*-fix-metadata*.diff`.
  Change `rust2rpm.toml` or `cargo-toml-edits.toml`, then run `$T regen <crate>`.
- **Never drop a required (non-optional) dependency.** Package it instead. Only
  optional dependencies, the features that need them, and dev-dependencies may
  be dropped.
- **Every Cargo.toml edit and every disabled test needs a comment** stating the
  real reason: `cargo-toml-patch-comments` for edits, `[tests] comments` for
  tests. Replace generated drafts and all `TODO` text. Never leave `TODO` in a
  finished package.
- **Prefer the newest crates.io release.** If a consumer pins an older minor
  version (e.g. `^0.57` while 0.58 exists), test the consumer against the new
  version. If it works, patch the consumer's requirement rather than packaging
  the old version.
- **Keep tests running where possible.** Prefer `[set-dev-version]` to Fedora's
  version over dropping a dev-dependency. Prefer skipping individual tests over
  disabling a whole target. Disable a target when it cannot compile, needs data
  missing from the crate, or takes too long.
- **Do not decide these alone; report them to the user:**
  - a dropped *default* feature with no obvious equivalent
  - UPDATE of a crate Fedora already ships (it affects other Fedora packages)
  - a required dependency lacking features in Fedora
  - deleting existing packages
- **Public actions need the user's explicit approval each time:** creating a
  COPR project, submitting COPR builds (`copr` without `-n`), Koji scratch
  builds, and anything in
  Bugzilla (`review-request --file`, which files tickets or posts comments
  under the user's name). Show the plan or the drafts first; never pass
  `--file` on your own.

## Workflow

1. **Scope:** `$T resolve <crate>…` or `$T resolve --manifest <path/Cargo.toml>`.
   Use `--json` for machine-readable output. Note NEW, UPDATE and FEATURES
   entries.
2. **Create:** `$T init --recursive <crate>…`. It prints `ACTION NEEDED:` lines
   on stderr, and a `Suggested [tests] table` for each package.
3. **For each package**, until `regen` prints no `ACTION NEEDED`:
   - Resolve each `ACTION NEEDED` using the table in `references/MANUAL.md`. Typical fixes:
     - set `summary`
     - choose `add-default-features`
     - try `set-dev-version`
     - for a crate that links a C library (`links =`, or `pkg-config`,
       `bindgen`, `cc` build-dependencies): read `build.rs` and add the
       library to `[requires] build` *and* `lib`, e.g.
       `"pkgconfig(openssl) >= 3.0.7"`. `build.rs` runs again in every
       dependent crate's build, so the -devel package needs it too. Do not add
       what Fedora's crates already pull in (bindgen brings `clang-libs`).
     - a missing license file whose lookup failed: the crate's `repository`
       may be a stale mirror. Find the repository that has the commit from
       `.cargo_vcs_info.json` and use its raw URL as the extra source.
   - Rewrite the draft `cargo-toml-patch-comments` so each says precisely what
     was changed and why.
   - `$T regen <crate>`
4. **Tests**, for each package:
   - `$T trial --discover --apply <crate>`
   - Inspect the log it names (`~/.cache/rust-packaging-tools/trial/<crate>.log`)
     to learn *why* each target fails.
   - Write the reasons into `[tests] comments`.
   - Tests that need the network fail in `trial` exactly as in mock. Skip them
     individually with `skip.<target>`, and state the reason. Loopback
     (`127.0.0.1`, `::1`) works in both, so a test that only talks to a local
     server is not a network test. `--apply` re-checks into
     `<crate>.recheck.log` and keeps the discovery log.
   - `$T regen <crate>`, then `$T trial <crate>`, which must pass.
   - If a whole target fails only because of one or two tests, switch to
     `skip.<target>` with `skip-exact.<target> = true` instead of dropping the
     target.
5. **SRPMs:** `$T srpm --all` (or the list of crates). Every package must show
   `%prep ok` and `0 errors`.
6. **Order and build:** `$T order --all`.
   - If the user can run mock (being in the `mock` group, no password prompt),
     run `$T mock-chain --all -r fedora-<N>-x86_64`. Use the Fedora release
     whose repositories `resolve` queried (the host's, unless
     `RUST_DEPS_DNF_ARGS` changed it). It is the only authoritative check that
     the packages build against Fedora's crate versions. Runs take long, so
     run it in the background.
   - When a build fails, read `root.log` (dependency problems) and `build.log`
     (compile and test errors) in the results directory mock prints. Fix the
     package, `regen`, `trial`, `srpm`, then resume the chain with only the
     remaining packages. The local repository keeps what already built.
   - Otherwise print `$T mock-chain --all -n` and the COPR command
     (`$T copr --all --project <p> -r <chroot> -n`) for the user.
7. **Review:** after `mock-chain` succeeded, run `$T review --all -r <same
   chroot>` (in the background; it rebuilds every package in mock). It runs
   `fedora-review` with the local dependencies from the mock-chain results.
   - `[~]` lines are failed checks known to be expected for rust2rpm specs.
   - Fix every `[!]` line in `rust2rpm.toml` or upstream, then `regen`,
     `srpm`, `mock-chain` that package and `review` it again.
   - Go through the `[ ]` manual items of each `review.txt` with the table in
     `references/MANUAL.md` ("Package review"), and report the answers that
     are specific to the package (license findings, bundled code, disabled
     tests, license workarounds).
   - Without mock, say that the review was not run.
8. **Submit to Fedora** (only when the user wants the packages in Fedora):
   follow "Submitting to Fedora" in `references/MANUAL.md`.
   - Ask for the FAS account name, the COPR project (`owner/project`), and
     whether the user is in the `packager` group (else `--needs-sponsor`).
   - With approval, `$T copr --all --project <p> -r fedora-rawhide-x86_64
     --wait`.
   - `$T review-request --all --project <p> --fas <name>` writes drafts to
     `<crate>/review-request.txt`. Show them to the user. The review bot may
     not respond (it has not since 2026-08-28), so the request carries its
     own build evidence: the COPR build link, and for a user in the
     `packager` group, with approval, a Koji scratch build
     (`--koji-task <crate>=<task>`).
   - With approval, the same command with `--file` files the tickets in
     dependency order (or posts updated URLs to existing ones). Give the user
     the ticket URLs.
   - `$T review-status --all` (read-only) shows each ticket's state, the
     reviewers' comments that were not answered yet, open NEEDINFO requests,
     the review bot's result, and the next step. Run it whenever the user asks
     about the reviews. Read the full thread in
     `<crate>/review-bug-<id>.txt` before acting on a comment, and summarize
     for the user what each reviewer asks.
   - A ticket with `fedora-review+`, in RELEASE_PENDING (set when the dist-git
     repository is created) or CLOSED is a **finished review**: the package
     is in, or being imported into, Fedora. Do not analyse its comments or
     review bot results, and do not propose changes to it through the review;
     the only steps left are those `review-status` names (import and build,
     then close the ticket). Remarks a reviewer left with the approval are
     handled later as normal dist-git updates, if the user wants them. Only a
     new comment on such a ticket may need attention.
   - For all review tickets of a person (e.g. the user's FAS/Bugzilla login),
     including ones outside the packages tree, use `$T review-status --user
     <login>`. It remembers the last check in
     `~/.cache/rust-packaging-tools/review-status/<login>/`, so report what is
     new since then first.
   - After a reviewer's comments: fix, `regen`, `srpm`, `review`, `copr
     --wait`, then `review-request --file --comment "<what changed>"`.
     Replying in Bugzilla in other ways is up to the user.
   - After approval (`fedora-review+`), give the user the `fedpkg
     request-repo`/`import`/`build` steps from the manual, in `order` stages.
9. **Consumer:** if the crates were needed by a project (like `authz-details-rs`
   needing jsonschema), re-check it with `$T resolve --manifest … --local-root <tree holding its sibling crates>`. Expect "all
   available" or only the new local packages. Patch its spec requirement if you
   bumped a version.

## Packaging inside the upstream repository

When the specs live in the crate's own repository (as `rust2rpm.toml` +
`rust-<crate>.spec` next to `Cargo.toml`), keep the upstream layout and style
(e.g. `Release: 1%{?dist}` if the existing specs use it), and run rust2rpm in
the crate directory. For a version that is not on crates.io yet:

- `cargo package --no-verify --workspace` packages all workspace crates
  against each other. Packaging a single crate that depends on an unpublished
  sibling fails.
- `$T regen <crate> --crate-file target/package/<crate>-<ver>.crate` (or
  `rust2rpm --path <abs path to .crate>` directly) generates the spec from
  that local crate.
- To test a release before it is tagged, apply the version bump in a
  throwaway `git worktree`, build the SRPMs with `rpmbuild -bs` from the
  local crates, and `mock --chain` them.

## Report back

Make every bug and build a link the user can open: write Bugzilla tickets
as `[2536992](https://bugzilla.redhat.com/2536992)`, comments as
`[#3](https://bugzilla.redhat.com/show_bug.cgi?id=2536992#c3)`, COPR builds
as `[10916293](https://copr.fedorainfracloud.org/coprs/build/10916293)`, and
Koji builds and tasks with their `buildinfo`/`taskinfo` URL. `rust-deps`
prints these URLs; never report a bare bug or build number.

- **Packages created or updated,** with versions and build stages from `order`.
- **Each Cargo.toml edit and each disabled test,** one line each, with the reason.
- **What was verified:** trial, srpm/prep, mock, fedora-review. Say plainly
  when mock or the review was not run.
- **Review findings:** each `[!]` fixed or left open, and the package-specific
  answers to the manual review items.
- **Submission:** COPR builds, and for each package its review request (draft
  path, or ticket URL once filed).
- **Open decisions** the user must make: UPDATE of Fedora packages, default
  features, compat packages.
