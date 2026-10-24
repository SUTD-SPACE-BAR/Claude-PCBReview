"""Session-wide fixtures for the engine suite.

Two things, and both exist so that a stranger holding nothing but the zip gets
a green run.

The solve cache must never be the user's
----------------------------------------
`solvestore` defaults to the OS cache directory, which is right for a review
and wrong for a test run — a suite that wrote there would leave state behind,
would be answerable by a previous run's results, and would make "does a cold
review still produce this number" unanswerable on a developer's own machine.

So the whole session gets its own empty cache under `tmp_path_factory`. Note
what that is *not*: it is not switching the cache off. The tests exercise the
cached path exactly as a review does, starting cold and warming as they go,
because a cache that is bypassed in testing is a cache nobody has tested.

Boards are asked for by role, and named for readability
------------------------------------------------------
`boards.py` holds the registry and the whole argument for it: no board ships
with this tool and none can, so a board-dependent test asks for a *role* and
skips cleanly when nothing fills it.

The `boards` fixture below is a thin readability layer over that. Several
suites read better naming a board than naming a role — `boards["simple"]` in a
test about a two-layer board says less than the role does — so the fixture is a
mapping whose lookup **is** `boards.board_path(role)`, skip and all. It adds no
registry of its own and knows no paths.
"""

from __future__ import annotations

import os

import pytest

from pcbreview import solvestore
from tests import boards as boards_module


@pytest.fixture(scope="session", autouse=True)
def hermetic_solve_cache(tmp_path_factory):
    directory = tmp_path_factory.mktemp("solve-cache")
    previous = os.environ.get(solvestore.CACHE_ENV)
    os.environ[solvestore.CACHE_ENV] = str(directory)
    try:
        yield directory
    finally:
        if previous is None:
            os.environ.pop(solvestore.CACHE_ENV, None)
        else:
            os.environ[solvestore.CACHE_ENV] = previous


class _Boards:
    """`boards[role]` -> the board's path, or skip this test.

    Deliberately not a `dict`. A dict would have to be built at fixture time,
    which means resolving every role a suite might want and skipping on the
    strength of one that is missing — so a test needing only the two-layer
    board would skip because nobody registered a six-layer one.
    """

    def __getitem__(self, role: str):
        return boards_module.board_path(role)

    def get(self, role: str):
        """The path if the role is installed, `None` otherwise. No skip."""
        entry = boards_module.REGISTRY.get(role)
        return entry.pcb if entry is not None and entry.installed() else None

    def __contains__(self, role: str) -> bool:
        return boards_module.installed(role)


@pytest.fixture(scope="session")
def boards() -> _Boards:
    return _Boards()
