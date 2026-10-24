"""A warm process pool for the field solver.

Why this exists
---------------
`fields.solve` is 90% of a survey's cost and 85% of a whole audit's: the
four-layer board spends roughly 477 of its 560 seconds inside sparse LU
factorisations. Profiling — recorded in development notes that do not ship —
withdrew every other optimisation that was proposed: the mesh refinement is
already tuned, the margin ratio is a dead lever, the geometry prefilters are
single-digit percentages, and `SolveCache` already runs at a 99% hit rate.
There is no algorithmic slack left. The board is simply doing thousands of
genuinely expensive solves, and the only honest way to make that faster is to
run them on more cores.

Each solve is a pure function of its cross-section, so distributing them
changes nothing numerically. That was checked rather than assumed: 48 distinct
cross-sections solved serially and across a pool compare equal at ``repr()``
level, bit for bit.

Why *warm* is the whole point
-----------------------------
Windows process spawn plus the numpy/scipy/shapely import costs 200-400 ms per
worker. A pool created per batch spends more time starting than solving and
gets 1.7x; a pool created once and kept for the session gets 8.3x on the same
work. So the executor is created once, every worker is made to import and solve
before any real work is dispatched, and it lives until the audit ends.

Where it sits
-------------
**Behind `crosstalk.SolveCache`, never in front of it.** `audit()` shares one
cache between the crosstalk and broadside checks and that sharing is worth
about 300 s on the four-layer board — per-worker caches would throw it away to
buy back less. So a lookup still hits one central dictionary; only a *miss*
reaches this module.
"""

from __future__ import annotations

import contextlib
import os
from concurrent.futures import Future, ProcessPoolExecutor

#: Set to 1 to force the in-process path — a single-core machine, a debugger,
#: or anyone bisecting a numerical difference.
WORKERS_ENV = "PCBREVIEW_SOLVE_WORKERS"

#: `ProcessPoolExecutor` refuses more than this on Windows.
MAX_WORKERS = 61


def default_workers() -> int:
    """How many workers to run, unless the caller or the environment says.

    ``cpu_count - 1`` leaves a core for the process doing the geometry walk,
    which is not idle while the pool solves — it is producing the very
    cross-sections the pool consumes.
    """
    override = os.environ.get(WORKERS_ENV)
    if override:
        try:
            return max(1, min(MAX_WORKERS, int(override)))
        except ValueError:
            pass
    return max(1, min(MAX_WORKERS, (os.cpu_count() or 1) - 1))


def refuse_outside_main_process() -> None:
    """Stop a worker from starting a pool of its own. The fork-bomb guard.

    On Windows `ProcessPoolExecutor` *spawns*, and a spawned worker re-imports
    the parent's ``__main__``. A scratch script that drives an audit at module
    level rather than under ``if __name__ == "__main__":`` therefore has every
    worker re-run the whole script — and each of those spawns nineteen more.

    The symptom is not a crash. It is twenty `python.exe` at high CPU and a run
    taking three times as long, which looks exactly like ordinary contention;
    two sessions of this project lost about eighty minutes between them
    misreading it that way (HANDOFF §15A.2b, which asks for precisely this
    check and says why a warning buried in a long file was not enough).

    A worker has no business owning a pool under any circumstances, so this
    costs nothing legitimate. `SolveCache._start_pool` swallows the error and
    solves in process, so the bomb degrades into one slow review instead of an
    exponential one — and the message says where to look.
    """
    import multiprocessing

    if multiprocessing.current_process().name != "MainProcess":
        raise RuntimeError(
            "a solve pool must not be started inside a worker process. On "
            "Windows this means the script that drove this audit did its work "
            "at module level: put it under `if __name__ == \"__main__\":`. "
            "See HANDOFF §15A.2b.")


def _warm_worker(_ignored) -> bool:
    """Import the numeric stack and solve something trivial, in the worker.

    Dispatched once per worker before any real cross-section, so the 200-400 ms
    import is paid in parallel and up front rather than inside the first batch.
    The section is deliberately tiny and coarsely meshed: the point is the
    imports and the first-call warmup inside scipy, not the answer.
    """
    from pcbreview import crosstalk, fields  # noqa: F401  (import is the point)

    fields.microstrip_z0(0.25, 0.2, 0.035, 4.5, refinement=8.0)
    return True


def solve_matrices(section, refinement: float):
    """Solve one cross-section down to the two capacitance matrices.

    Runs in a worker. What crosses the process boundary is nine floats rather
    than a mesh, which was always the point — but they are the *matrices* now
    and not the ten coefficients derived from them, for two reasons:

    * `solvestore` keeps the matrices, because impedance and coupling are both
      arithmetic over them and one stored solve should serve either;
    * the reduction then happens in exactly one place, in the parent, on both
      the solved and the cached path. Two copies of that arithmetic is one
      copy too many for something whose byte-identity across a warm and a cold
      review is the property being claimed.
    """
    from pcbreview import fields

    solution = fields.solve(section, refinement=refinement)
    return (solution.capacitance, solution.capacitance_air,
            solution.mesh_nodes)


class SolvePool:
    """A live pool of warm solver processes.

    Deliberately thin. It owns process lifetime and nothing else: no cache, no
    knowledge of cross-sections, no ordering. `SolveCache` stays the single
    place that decides whether a solve is needed at all.
    """

    def __init__(self, workers: int | None = None, warm: bool = True):
        refuse_outside_main_process()
        self.workers = workers if workers is not None else default_workers()
        self._executor: ProcessPoolExecutor | None = None
        if self.workers <= 1:
            # Pool size 1 is the documented fallback to the in-process path.
            # Not "a pool with one worker" — that would add pickling and a
            # process hop to buy nothing.
            return
        self._executor = ProcessPoolExecutor(max_workers=self.workers)
        if warm:
            self.warm()

    @property
    def enabled(self) -> bool:
        return self._executor is not None

    def warm(self) -> None:
        """Force every worker to exist and to have imported the numeric stack.

        `map` over exactly `workers` items does not *guarantee* one task per
        worker, but `ProcessPoolExecutor` spins up a new process per pending
        item until it reaches the limit, so in practice this both creates and
        loads all of them. Getting it slightly wrong is harmless — a worker
        that missed the warmup pays its import on its first real solve.
        """
        if self._executor is None:
            return
        list(self._executor.map(_warm_worker, range(self.workers)))

    def submit(self, function, *args) -> Future:
        if self._executor is None:
            raise RuntimeError("pool is not enabled; call the function directly")
        return self._executor.submit(function, *args)

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    def __enter__(self) -> "SolvePool":
        return self

    def __exit__(self, *exc_info) -> None:
        self.shutdown()


@contextlib.contextmanager
def attached(cache, workers: int | None = None):
    """Let `cache` start a warm pool if the board turns out to need one.

    A context manager rather than a constructor argument because the processes
    have to be cleaned up on the way out, including when a check raises.

    What is handed over is a *factory*, not a pool. Starting nineteen workers
    costs about three seconds, and a two-layer board needs 52 solves in total —
    so an eagerly-started pool made the small boards slower than no pool at
    all. `SolveCache` calls the factory only once it has met
    `SolveCache.POOL_THRESHOLD` distinct cross-sections, by which point the
    board has proved it has enough work to repay the cost.

    Two cases pass through untouched, both deliberate:

    * a cache that already carries a pool — the caller is managing it;
    * a worker count of one — the in-process path, byte-identical and simpler.

    A pool that cannot be *started* is handled in `SolveCache._start_pool`
    rather than here, for the same reason: spawning processes can fail for
    causes that have nothing to do with this board — a sandbox, an exhausted
    handle table, a frozen build without `multiprocessing` — and refusing to
    review a PCB over that would be absurd. It reviews it slowly instead,
    which is exactly what it did before this module existed.
    """
    if cache.pool is not None or (workers if workers is not None
                                  else default_workers()) <= 1:
        yield None
        return

    cache.pool_factory = lambda: SolvePool(workers)
    try:
        yield None
    finally:
        # Drain first: futures outstanding when a check raises would otherwise
        # be cancelled underneath a cache that already recorded them as solved.
        cache.resolve(ignore_errors=True)
        pool, cache.pool, cache.pool_factory = cache.pool, None, None
        if pool is not None:
            pool.shutdown()
