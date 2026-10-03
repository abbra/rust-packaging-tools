"""rust-deps srpm: build the source RPM and check %prep."""

from __future__ import annotations

import sys
import tempfile

from . import packages
from . import util


def cmd_srpm(args) -> None:
    failed = []
    for pkg in packages.select(args.root, args.crates, args.all):
        util.info(f"== {pkg.crate} {pkg.version}")
        res = util.run(
            ["spectool", "-g", "-C", str(pkg.dir), str(pkg.spec)], capture_output=True
        )
        if res.returncode:
            failed.append(pkg.crate)
            print(res.stdout + res.stderr, file=sys.stderr)
            continue
        for old in pkg.dir.glob(f"{pkg.rpm_name}-[0-9]*.src.rpm"):
            old.unlink()
        res = util.run(
            [
                "rpmbuild",
                "-bs",
                "--define",
                f"_sourcedir {pkg.dir}",
                "--define",
                f"_srcrpmdir {pkg.dir}",
                str(pkg.spec),
            ],
            capture_output=True,
        )
        if res.returncode:
            failed.append(pkg.crate)
            print(res.stdout + res.stderr, file=sys.stderr)
            continue
        wrote = res.stdout.strip().splitlines()
        util.info(
            "   "
            + (wrote[-1] if wrote else f"Wrote: {pkg.rpm_name}-{pkg.version} src.rpm")
        )
        if not args.no_prep:
            with tempfile.TemporaryDirectory() as top:
                res = util.run(
                    [
                        "rpmbuild",
                        "-bp",
                        "--nodeps",
                        "--define",
                        f"_topdir {top}",
                        "--define",
                        f"_sourcedir {pkg.dir}",
                        str(pkg.spec),
                    ],
                    capture_output=True,
                )
            if res.returncode:
                failed.append(pkg.crate)
                print(res.stdout[-3000:] + res.stderr, file=sys.stderr)
                continue
            util.info("   %prep ok")
        res = util.run(["rpmlint", str(pkg.spec)], capture_output=True)
        summary = (res.stdout.strip().splitlines() or ["rpmlint: no output"])[-1]
        util.info(f"   {summary}")
    if failed:
        util.die(f"srpm failed for: {', '.join(failed)}")
