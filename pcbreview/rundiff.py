"""What moved since the last review — the design brief §2.9's banner.

> On a re-review this is more useful than the ranked list, and it is why the
> manifests must never be silently regenerated.

The engine already computes it. `review.compare()` diffs two runs keyed on
(check, nets, title) — deliberately **not** on score, because a finding whose
score moved by 0.1 is the same finding and not a loss and a gain. This module
is the wiring, and one ruling that is not the engine's to make.

The ruling: this reads, and does not write
------------------------------------------
`review.record()` appends a run to the folder. Calling it on every app launch
would mean opening a board to look at it silently rewrites its review history,
and the folder is the user's — the engine's own module docstring quotes them:
*"It definitely shouldnt randomly hoard findings somewhere."* Phase 1 made the
same call and left `record_review` uncalled for the same reason.

So the app **shows** the comparison and offers to record; recording happens
only when the user says so. `record()` here is that path, and nothing calls it
without an explicit request.
"""

from __future__ import annotations

from pathlib import Path

from pcbreview import review as review_module                 # noqa: E402


def _findings_payload(result) -> list[dict]:
    """This run's findings in the shape `review.compare` keys on.

    `Finding.summary()` is the engine's own machine-readable view and is what
    `record_review` writes, so a comparison built from it compares like with
    like against anything already in the folder.
    """
    return [f.summary() for f in result.layout.findings]


def since_last_run(result) -> dict:
    """The run-diff banner, or a stated reason there is not one.

    Four outcomes, and they are different things a reader must be able to tell
    apart:

    * `state="no_folder"` — the board has no `review/` folder, so there is
      nothing to compare against and nothing has gone wrong;
    * `state="first_run"` — a folder exists but holds no previous run;
    * `state="unreadable"` — a folder exists and its record could not be read;
    * `state="compared"` — with `resolved`, `new` and `changed`.

    A single empty diff for all four would read as "nothing changed", which is
    false in three of them.
    """
    board_path = result.board_path or result.project.board
    root = review_module.find(board_path)
    if root is None:
        return {"state": "no_folder", "folder": str(
            review_module.folder_for(board_path))}

    previous = review_module.previous(root)
    if previous is None:
        latest = Path(root) / "latest.json"
        if latest.exists():
            # `review.previous` swallows a corrupt record and returns None so a
            # bad file cannot stop a review. That is right for the engine and
            # wrong to render as "first run" — the file is there.
            return {"state": "unreadable", "folder": str(root),
                    "why": f"{latest.name} exists but could not be read"}
        return {"state": "first_run", "folder": str(root)}

    diff = review_module.compare(previous, _findings_payload(result))
    diff.update({
        "state": "compared", "folder": str(root),
        "artefacts": review_module.changed_since(
            root, result.layout.artefacts or {}),
    })
    return diff


def record(result) -> dict:
    """Append this run to the board's `review/` folder. **Only on request.**

    Creates the folder if it is not there, which is why nothing may call this
    on a user's behalf: `review.create` writes a README into their project
    directory, and a directory appearing in someone's project without them
    asking is exactly what the engine's ruling is against.
    """
    board_path = result.board_path or result.project.board
    root = review_module.find(board_path) or review_module.create(board_path)
    written = review_module.record(
        root, result.layout.board, result.layout.artefacts or {},
        result.layout.summary(), _findings_payload(result))
    return {"recorded": True, "path": str(written), "folder": str(root)}
