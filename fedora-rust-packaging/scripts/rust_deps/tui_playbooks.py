"""rust-deps TUI playbooks: what each command's screen does."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import tui_results

TUI_RESULT_RENDERERS = {
    "trial": tui_results.render_trial_output,
    "srpm": tui_results.render_srpm_output,
    "status": tui_results.render_status_output,
    "doctor": tui_results.render_doctor_output,
    "copr-status": tui_results.render_copr_status_output,
    "review-plan": tui_results.render_review_plan_output,
    "review-request": tui_results.render_review_request_output,
    "resolve": tui_results.render_resolve_output,
    "update": tui_results.render_update_output,
    "order": tui_results.render_order_output,
}


@dataclass
class TuiPlaybook:
    """What one command's screen does, as data: 'result' names the renderer
    that shapes its output into a table and next steps ('' keeps the generic
    log view); 'requires' lists tools 'doctor' reports, and the sidebar dims
    the command while one is missing; 'carry' are form values the previous run
    established (the COPR project, the target chroot) that this form keeps."""

    result: str = ""
    requires: tuple[str, ...] = ()
    carry: tuple[str, ...] = ()


TUI_PLAYBOOKS = {
    "doctor": TuiPlaybook(result="doctor"),
    "status": TuiPlaybook(result="status", carry=("target",)),
    "resolve": TuiPlaybook(result="resolve"),
    "order": TuiPlaybook(result="order"),
    "check-targets": TuiPlaybook(carry=("chroot", "project")),
    "init": TuiPlaybook(carry=("target",)),
    "regen": TuiPlaybook(),
    "update": TuiPlaybook(result="update"),
    "trial": TuiPlaybook(result="trial"),
    "srpm": TuiPlaybook(result="srpm"),
    "mock-chain": TuiPlaybook(requires=("mock",)),
    "copr": TuiPlaybook(requires=("copr-cli",), carry=("project", "chroot")),
    "copr-log": TuiPlaybook(requires=("copr-cli",), carry=("project",)),
    "copr-status": TuiPlaybook(
        result="copr-status", requires=("copr-cli",), carry=("project", "chroot")
    ),
    "tmt": TuiPlaybook(requires=("mock",), carry=("project",)),
    "review": TuiPlaybook(requires=("fedora-review", "mock"), carry=("chroot",)),
    "review-request": TuiPlaybook(result="review-request", carry=("project", "chroot")),
    "review-plan": TuiPlaybook(result="review-plan", carry=("project", "chroot")),
    "review-status": TuiPlaybook(),
    "adopt": TuiPlaybook(),
    "dist-git": TuiPlaybook(requires=("fedpkg",)),
}
