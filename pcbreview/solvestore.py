"""A persistent, content-addressed cache for field solves.

Why this exists
---------------
`fields.solve` is 85-90% of an audit. `crosstalk.SolveCache` and
`routed._SolveCache` already collapse thousands of requests onto the distinct
cross-sections behind them — 99% of lookups are hits — but both die with the
process, so **every review starts from zero**. The four-layer board pays ~477
of its 560 seconds inside sparse LU factorisations it has performed many times
before, on geometry that has not changed.

That is the wrong shape for the tool this is meant to be. A review you run once
at the end can afford to be cold; a review you run *while working* cannot. Move
three traces on the V3 board and roughly 3 690 of its ~3 700 cross-sections are
bit-for-bit the ones solved last time.

So this module stores the solver's output on disk, keyed by a hash of
everything that went into it.

What is cached, and why it is that and not the answer
-----------------------------------------------------
The **capacitance matrices**, not the numbers derived from them. `fields.solve`
returns ``C``, ``C_air`` and a mesh node count; impedance, epsilon_eff,
crosstalk coefficients and delay are all pure arithmetic over those. Caching the
primitive means one stored solve serves `routed`'s impedance and `crosstalk`'s
coupling coefficients alike, and it means a change to *how a number is
presented* — a different edge rate, a new derived quantity — costs nothing and
invalidates nothing. the caching note §1 states the principle; this is it.

The value is small: two square matrices of 1 or 2 signal conductors, so 8 to 72
bytes of float64 plus a node count.

The failure mode that governs the whole design
-----------------------------------------------
> *"if the key omits anything affecting the result, the cache silently serves a
> wrong number, which is the worst failure mode this project has."* — the
> caching note, stating the rule this module is built to hold.

Four things follow, and they are the reason this module is longer than a
dictionary with a file behind it.

1. **The key covers the entire `CrossSection`**, field by field — every slab,
   every conductor in order, the bounding box, the feature size, and even the
   description string. Nothing is judged irrelevant. A field that does not
   affect the answer costs at most a duplicate row.

2. **The key covers the solver itself, by hashing its source.** Not a version
   integer someone has to remember to bump — `fields.py`'s bytes. An edit to
   the mesh, the assembly, a constant or a comment produces a different
   fingerprint and a cold cache. A comment costs one slow review; a forgotten
   bump costs a wrong number, and only one of those is recoverable.
   `numpy` and `scipy` versions are in there too, because the sparse LU is
   theirs.

3. **The shape of `CrossSection` is checked at import.** If a field is added to
   `CrossSection`, `Slab` or `Conductor` and this module is not taught about
   it, `_shape_is_known` returns False and **the cache disables itself** rather
   than key on a stale idea of what a cross-section is. `test_solvestore.py`
   asserts the same thing, so the failure is a red test and not a silent one.

4. **`verify()` recomputes.** Take stored entries, solve them again in this
   process, and require equality. `python -m pcbreview.audit … --verify-cache`
   does this against the sections a real board actually asks for, which is a
   stronger check than a synthetic sample: it exercises the key derivation on
   the same path the audit uses.

What is *not* stored — and why that is a privacy property, not an omission
--------------------------------------------------------------------------
Rows are ``sha256 -> floats``. The cross-section itself is **never written**.
So the file cannot be read back into anybody's geometry: it does not contain
trace widths, layer heights or net names, and there is nothing in it to
attribute to a board or a customer. `review.py` records the ruling that made
that matter — an installed skill was found holding a 602 KB geometry dump of
another company's board — and the cheapest way to honour it here was to make
the cache incapable of holding one.

It is also why this cache is allowed to be **shared across projects**, which
the per-board `review/.cache` cannot be. Two boards on the same JLC stackup
solve the same cross-sections; the second one starts warm.

Where it lives
--------------
The OS's own cache directory — ``%LOCALAPPDATA%`` on Windows,
``~/Library/Caches`` on macOS, ``$XDG_CACHE_HOME`` or ``~/.cache`` elsewhere.
Never the install directory, and never the user's project folder: `review.py`
rules out both, and neither would give the cross-project sharing that is half
the point.

It is bounded. `MAX_ENTRIES` rows are kept and the least *recently used* are
pruned — reading a row moves its stamp, batched, so a cross-section reused on
every review outlives one stored once and never wanted again. That is about
20 MB. ``PCBREVIEW_SOLVE_CACHE`` names a different path, or the word
``off`` to disable the whole thing.

Failing is never fatal
----------------------
An unwritable directory, a missing `sqlite3`, a corrupt file, a locked database:
all of them return `None` from `open_default()` and the review runs exactly as
it did before this module existed. Slow is an answer; refusing to review a PCB
because a cache file would not open is not. `solvepool._start_pool` takes the
same position for the same reason.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from pcbreview import fields

#: Point at another file, or set to `off`/`none`/`0` to disable entirely.
CACHE_ENV = "PCBREVIEW_SOLVE_CACHE"

#: Bumped by hand only when the *storage* format changes — the solver's own
#: identity is hashed, not versioned, so this does not track it.
FORMAT_VERSION = 1

#: Rows kept before the least recently used are pruned. ~200 bytes a row.
MAX_ENTRIES = 100_000

#: Prune once the table has grown this far past `MAX_ENTRIES`, so a review does
#: not pay for a delete on every insert.
PRUNE_SLACK = 1.15

#: Pending writes held before a commit. WAL plus `synchronous=NORMAL` makes a
#: commit cheap, but not free, and a board produces thousands of them.
WRITE_BATCH = 256

_FILE_NAME = "solves.sqlite3"


# ---------------------------------------------------------------------------
# What the solver's output looks like on the way to and from disk
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Primitive:
    """One solved cross-section, reduced to what cannot be recomputed.

    Everything `FieldSolution` offers — impedance, epsilon_eff, inductance,
    the crosstalk coefficients — is arithmetic over these two matrices. The
    node count is carried because `fields.solve` reports a warning above a
    threshold and the reconstruction has to be able to say the same thing.
    """
    capacitance: np.ndarray
    capacitance_air: np.ndarray
    mesh_nodes: int

    def to_blob(self) -> bytes:
        """Raw float64, not text. A formatted float is a lossy float."""
        first = np.ascontiguousarray(self.capacitance, dtype="<f8")
        second = np.ascontiguousarray(self.capacitance_air, dtype="<f8")
        if first.shape != second.shape or first.ndim != 2 \
                or first.shape[0] != first.shape[1]:
            raise ValueError("capacitance matrices must be square and alike")
        header = struct.pack("<HHI", FORMAT_VERSION, first.shape[0],
                             int(self.mesh_nodes))
        return header + first.tobytes() + second.tobytes()

    @classmethod
    def from_blob(cls, blob: bytes) -> "Primitive | None":
        """`None` for anything that does not decode. A cache miss, not a crash."""
        if len(blob) < 8:
            return None
        version, order, nodes = struct.unpack("<HHI", blob[:8])
        if version != FORMAT_VERSION or order < 1:
            return None
        span = order * order * 8
        if len(blob) != 8 + 2 * span:
            return None
        first = np.frombuffer(blob, dtype="<f8", count=order * order, offset=8)
        second = np.frombuffer(blob, dtype="<f8", count=order * order,
                               offset=8 + span)
        # `frombuffer` is a read-only view onto the row; callers pass these
        # into numpy that has no business being handed something immutable.
        return cls(capacitance=first.reshape(order, order).copy(),
                   capacitance_air=second.reshape(order, order).copy(),
                   mesh_nodes=int(nodes))

    @classmethod
    def of(cls, solution: fields.FieldSolution) -> "Primitive":
        return cls(capacitance=solution.capacitance,
                   capacitance_air=solution.capacitance_air,
                   mesh_nodes=solution.mesh_nodes)

    def solution(self, section: fields.CrossSection) -> fields.FieldSolution:
        """Back into the object `fields.solve` would have returned.

        Built by `fields.solution_from` rather than here, so the warnings a
        reconstructed solution carries are produced by the same code that
        produces them on the solved path and cannot drift from it.
        """
        return fields.solution_from(section, self.capacitance,
                                    self.capacitance_air, self.mesh_nodes)

    def matches(self, other: "Primitive") -> bool:
        """Bit-for-bit. `verify()` accepts nothing looser."""
        return (self.mesh_nodes == other.mesh_nodes
                and np.array_equal(self.capacitance, other.capacitance)
                and np.array_equal(self.capacitance_air,
                                   other.capacitance_air))


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------

#: Every field of the three dataclasses that make up a cross-section, as this
#: module understands them. Checked against the real ones at import — see
#: `_shape_is_known`. This tuple is the contract, and growing a dataclass
#: without growing this turns the cache off instead of keying on a stale idea
#: of what a cross-section is.
_KNOWN_SHAPE = {
    "CrossSection": ("slabs", "conductors", "x_left", "x_right", "y_bottom",
                     "y_top", "feature_mm", "description"),
    "Slab": ("y0", "y1", "epsilon_r"),
    "Conductor": ("x0", "x1", "y0", "y1", "name", "is_ground"),
}


def _shape_is_known() -> bool:
    """Whether `fields`'s dataclasses are still the ones keyed below."""
    for name, expected in _KNOWN_SHAPE.items():
        cls = getattr(fields, name, None)
        if cls is None or not dataclasses.is_dataclass(cls):
            return False
        if tuple(f.name for f in dataclasses.fields(cls)) != expected:
            return False
    return True


#: Computed once. False means every `key_for` returns `None` and the cache is
#: inert — see the module docstring, point 3.
SHAPE_IS_KNOWN = _shape_is_known()


def _number(value: float) -> str:
    """A float as its exact hexadecimal form.

    `repr` round-trips too, but `float.hex` is unambiguous about the last bit
    and about the sign of zero without anyone having to know that it is.
    """
    return float(value).hex()


_fingerprint_cache: str | None | tuple = ()


def solver_fingerprint() -> str | None:
    """A hash of the solver: `fields.py`'s source, and numpy's and scipy's
    versions.

    Source rather than a version constant, deliberately. A constant is only as
    good as whoever remembers to change it, and the cost of the two mistakes is
    wildly asymmetric: a needless bump is one cold review, a missed one is a
    wrong impedance served silently and for ever.

    `None` when the source cannot be read — a frozen build, an importer with no
    file behind it. The cache then declines to run, because it can no longer
    tell one solver from another.
    """
    global _fingerprint_cache
    if _fingerprint_cache != ():
        return _fingerprint_cache            # type: ignore[return-value]
    _fingerprint_cache = None
    try:
        source = Path(fields.__file__).read_bytes()
    except (OSError, TypeError, AttributeError):
        return None
    digest = hashlib.sha256()
    digest.update(source)
    for module in ("numpy", "scipy"):
        version = getattr(sys.modules.get(module), "__version__", None)
        if version is None:                                   # pragma: no cover
            try:
                import importlib
                version = getattr(importlib.import_module(module),
                                  "__version__", "?")
            except Exception:                                 # noqa: BLE001
                version = "?"
        digest.update(f"{module}={version}".encode())
    _fingerprint_cache = digest.hexdigest()
    return _fingerprint_cache


def canonical(section: fields.CrossSection, refinement: float,
              growth: float) -> bytes | None:
    """Every input to `fields.solve`, as one unambiguous byte string.

    Order is preserved and never sorted: `fields` treats conductor 0 as the
    victim, and a cross-section whose conductors were swapped is a different
    problem with a different answer.
    """
    fingerprint = solver_fingerprint()
    if fingerprint is None or not SHAPE_IS_KNOWN:
        return None
    parts = [
        f"pcbreview-solve/{FORMAT_VERSION}",
        f"solver={fingerprint}",
        f"refinement={_number(refinement)}",
        f"growth={_number(growth)}",
        f"x_left={_number(section.x_left)}",
        f"x_right={_number(section.x_right)}",
        f"y_bottom={_number(section.y_bottom)}",
        f"y_top={_number(section.y_top)}",
        f"feature_mm={_number(section.feature_mm)}",
        # `!r` so a newline or a quote inside a description cannot forge a
        # field boundary. Descriptions are built from geometry, so including
        # one costs no hit rate; excluding it would be a judgement about
        # relevance, and this key makes none.
        f"description={section.description!r}",
        f"slabs={len(section.slabs)}",
    ]
    for slab in section.slabs:
        parts.append(f"slab {_number(slab.y0)} {_number(slab.y1)} "
                     f"{_number(slab.epsilon_r)}")
    parts.append(f"conductors={len(section.conductors)}")
    for conductor in section.conductors:
        parts.append(
            f"cond {_number(conductor.x0)} {_number(conductor.x1)} "
            f"{_number(conductor.y0)} {_number(conductor.y1)} "
            f"{conductor.name!r} {int(bool(conductor.is_ground))}")
    return "\n".join(parts).encode("utf-8")


def key_for(section: fields.CrossSection, refinement: float,
            growth: float = fields.DEFAULT_GROWTH) -> bytes | None:
    """The 32-byte content address of one solve, or `None` if unkeyable."""
    material = canonical(section, refinement, growth)
    if material is None:
        return None
    return hashlib.sha256(material).digest()


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS solves (
    key    BLOB PRIMARY KEY,
    value  BLOB NOT NULL,
    made   REAL NOT NULL,
    used   REAL NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS solves_used ON solves(used);
"""


class SolveStore:
    """An sqlite table of `sha256 -> capacitance matrices`.

    Thread-safe by a single lock around a single connection, because the app
    can have a review running on a worker thread while the UI reads. The
    contention is irrelevant — a lookup is tens of microseconds against solves
    that are tens of milliseconds.

    Not process-safe in the sense of being *shared* by the solve pool: workers
    never touch it. The parent decides what to dispatch, and a dispatched
    cross-section is by definition one the store did not have.
    """

    def __init__(self, connection, path: Path | None = None):
        self._connection = connection
        self._lock = threading.Lock()
        #: written but not yet committed, and **consulted by `get`**. A dict
        #: rather than a list so a lookup is answered from it: without that,
        #: every `get` would have to flush first to stay consistent, and the
        #: batching that makes three thousand writes cheap would never happen.
        self._pending: dict[bytes, tuple] = {}
        #: keys served this session, waiting to have their `used` stamp moved.
        #: Batched rather than written per hit: a fully warm review is five
        #: thousand hits, and LRU is not worth five thousand UPDATEs on the
        #: one path this whole module exists to make fast.
        self._touched: set[bytes] = set()
        self.path = path
        self.hits = 0
        self.misses = 0
        self.writes = 0
        #: `--verify-cache` turns this on. Off, nothing is retained; on, every
        #: cross-section served from disk is kept **in memory** so it can be
        #: re-solved and compared. In memory and never written: the promise
        #: that this file holds no geometry is not conditional on a flag.
        self.retain = False
        self._retained: list[tuple] = []

    # -- lookup -------------------------------------------------------------

    def get(self, key: bytes | None) -> Primitive | None:
        if key is None:
            return None
        with self._lock:
            waiting = self._pending.get(key)
            if waiting is not None:
                row = (waiting[1],)
            else:
                try:
                    row = self._connection.execute(
                        "SELECT value FROM solves WHERE key = ?",
                        (key,)).fetchone()
                except Exception:                            # noqa: BLE001
                    return None
        if row is None:
            with self._lock:
                self.misses += 1
            return None
        primitive = Primitive.from_blob(row[0])
        if primitive is None:
            # A row this build cannot read is not a hit and not an error; the
            # solve happens and overwrites it.
            with self._lock:
                self.misses += 1
            return None
        with self._lock:
            self.hits += 1
            self._touched.add(key)
        return primitive

    def put(self, key: bytes | None, primitive: Primitive) -> None:
        if key is None:
            return
        try:
            blob = primitive.to_blob()
        except (ValueError, TypeError):
            return
        now = _now()
        with self._lock:
            self._pending[key] = (key, blob, now, now)
            self.writes += 1
            if len(self._pending) >= WRITE_BATCH:
                self._flush_locked()

    # -- housekeeping -------------------------------------------------------

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not (self._pending or self._touched):
            return
        pending, self._pending = list(self._pending.values()), {}
        # A key both written and read this session is already carrying a fresh
        # stamp from the insert, so it is dropped from the touch list rather
        # than updated twice.
        touched, self._touched = self._touched - {row[0] for row in pending}, set()
        now = _now()
        try:
            if pending:
                self._connection.executemany(
                    "INSERT INTO solves (key, value, made, used) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "used = excluded.used",
                    pending)
            if touched:
                self._connection.executemany(
                    "UPDATE solves SET used = ? WHERE key = ?",
                    [(now, key) for key in touched])
            self._connection.commit()
        except Exception:                                    # noqa: BLE001
            # A cache that cannot write is a cache that is slow, which is the
            # state this whole module exists to improve on and never worse
            # than. Losing the batch is the correct outcome.
            with contextlib.suppress(Exception):
                self._connection.rollback()

    def count(self) -> int:
        with self._lock:
            self._flush_locked()
            try:
                return int(self._connection.execute(
                    "SELECT count(*) FROM solves").fetchone()[0])
            except Exception:                                # noqa: BLE001
                return 0

    def prune(self, limit: int = MAX_ENTRIES) -> int:
        """Drop the least recently used rows down to `limit`. Returns how many.

        Called on close rather than on insert so a review never stops to tidy.
        """
        total = self.count()
        if total <= limit * PRUNE_SLACK:
            return 0
        with self._lock:
            try:
                self._connection.execute(
                    "DELETE FROM solves WHERE key IN ("
                    "  SELECT key FROM solves ORDER BY used ASC LIMIT ?)",
                    (total - limit,))
                self._connection.commit()
            except Exception:                                # noqa: BLE001
                return 0
        return total - limit

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
            self._touched.clear()
            with contextlib.suppress(Exception):
                self._connection.execute("DELETE FROM solves")
                self._connection.commit()

    def close(self) -> None:
        self.flush()
        self.prune()
        with self._lock:
            with contextlib.suppress(Exception):
                self._connection.close()

    # -- self-checking ------------------------------------------------------

    def record_for_verification(self, section, refinement: float,
                                growth: float) -> None:
        """Keep a served cross-section so `verify_served` can re-solve it."""
        if self.retain:
            self._retained.append((section, refinement, growth))

    @property
    def retained(self) -> int:
        return len(self._retained)

    def verify(self, sections, refinement: float,
               growth: float = fields.DEFAULT_GROWTH) -> dict:
        """Re-solve `sections` and require the stored answer to match exactly.

        The mitigation the caching note asks for. ``{"checked", "matched", "absent",
        "mismatched": [descriptions]}``.
        """
        return self._verify((s, refinement, growth) for s in sections)

    def verify_served(self) -> dict:
        """Re-solve **everything this run took off disk**, and require a match.

        The caching note proposed a random sample. Verifying the lot is strictly
        better and the cost is easy to state: it is the run you just saved.
        Someone who asked to verify the cache has already accepted that.

        Pointed at the sections a real board produced rather than at synthetic
        ones, which matters — an omitted key field bites on the geometry a
        board actually generates, and only the audit's own path generates it.
        """
        return self._verify(self._retained)

    def _verify(self, items) -> dict:
        result = {"checked": 0, "matched": 0, "absent": 0, "mismatched": []}
        for section, refinement, growth in items:
            stored = self.get(key_for(section, refinement, growth))
            if stored is None:
                result["absent"] += 1
                continue
            result["checked"] += 1
            fresh = Primitive.of(fields.solve(section, refinement=refinement,
                                              growth=growth))
            if stored.matches(fresh):
                result["matched"] += 1
            else:
                result["mismatched"].append(section.description
                                            or "(no description)")
        return result

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    def summary(self) -> str:
        return (f"solve cache: {self.hits} reused, {self.writes} stored, "
                f"{self.hit_rate:.0%} of {self.lookups} distinct "
                f"cross-section(s)")


def _now() -> float:
    import time
    return time.time()


# ---------------------------------------------------------------------------
# Opening one
# ---------------------------------------------------------------------------

def default_path() -> Path | None:
    """Where the shared cache lives, or `None` if it is switched off.

    ``PCBREVIEW_SOLVE_CACHE`` overrides: a path, or ``off``/``none``/``0``.
    """
    override = os.environ.get(CACHE_ENV, "").strip()
    if override:
        if override.lower() in ("off", "none", "no", "0", "disabled"):
            return None
        candidate = Path(override).expanduser()
        # A directory is a place to put the file; anything else is the file.
        if candidate.is_dir() or not candidate.suffix:
            return candidate / _FILE_NAME
        return candidate
    return _os_cache_dir() / "pcbreview" / _FILE_NAME


def _os_cache_dir() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local)
        return Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return Path(xdg) if xdg else Path.home() / ".cache"


def open_store(path: Path | str | None) -> SolveStore | None:
    """Open or create the cache at `path`. `None` on any failure at all.

    Every caller treats `None` as "review without a cache", so there is no
    error path out of here — an unwritable home directory, a database locked by
    another tool, a build with no `sqlite3`. All of them are a slower review.
    """
    if path is None:
        return None
    if solver_fingerprint() is None or not SHAPE_IS_KNOWN:
        # Cannot tell one solver, or one cross-section, from another.
        return None
    try:
        import sqlite3
    except ImportError:                                      # pragma: no cover
        return None
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), check_same_thread=False,
                                     timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        # A cache may lose the last few writes to a power cut. It may not be
        # allowed to make a review wait on an fsync three thousand times.
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(_SCHEMA)
        connection.commit()
    except Exception:                                        # noqa: BLE001
        return None
    return SolveStore(connection, path=path)


def open_default() -> SolveStore | None:
    return open_store(default_path())


# ---------------------------------------------------------------------------
# The ambient store
# ---------------------------------------------------------------------------
#
# `solvepool.attached` established the shape: a context manager that hands a
# capability to code deep inside the engine without threading a parameter
# through six signatures that do not care about it. This is the same idea, and
# it is off unless something switches it on — so a test, a convergence study or
# anyone calling `fields.solve` directly gets the solver and nothing else.

_ambient = threading.local()


def current() -> SolveStore | None:
    """The store this thread is running under, if any."""
    return getattr(_ambient, "store", None)


@contextlib.contextmanager
def attached(store: SolveStore | None = None, path=None, enabled: bool = True):
    """Make `store` the ambient cache for the duration.

    Nests: an inner `attached` is honoured and the outer one restored, so an
    audit inside an audit does not lose the cache the caller opened.

    Closes only what it opened. A store passed in belongs to the caller and
    outlives the block; one opened here is flushed, pruned and closed on the
    way out, which is where the bounded-size promise is actually kept.
    """
    owned = None
    if not enabled:
        store = None
    elif store is None:
        store = owned = open_store(path if path is not None else default_path())
    previous = current()
    _ambient.store = store
    try:
        yield store
    finally:
        _ambient.store = previous
        if owned is not None:
            owned.close()


@contextlib.contextmanager
def for_audit(request):
    """`audit`'s ``solve_store`` argument, resolved into an ambient store.

    The same three states every optional input to `audit_files` has —
    ``None`` for "find the usual one", a value for "use this one", ``False``
    for "there is none, and do not go looking". `audit_files`' docstring
    explains at length why that third state earns its keep; this argument gets
    it for the same reason, so a measurement can pin the cache off and know it
    did.

    ``None`` **defers to a store the caller already attached** rather than
    opening a second connection to the same file. The app opens one for the
    life of a session and audits inside it; without this, every review would
    open, prune and close its own.
    """
    if request is False:
        with attached(enabled=False):
            yield None
        return
    if isinstance(request, SolveStore):
        with attached(request):
            yield request
        return
    if request is not None:
        with attached(path=request) as store:
            yield store
        return
    existing = current()
    if existing is not None:
        yield existing
        return
    with attached() as store:
        yield store


# ---------------------------------------------------------------------------
# Serving one solve
# ---------------------------------------------------------------------------

def lookup(section: fields.CrossSection, refinement: float,
           growth: float = fields.DEFAULT_GROWTH
           ) -> tuple[bytes | None, fields.FieldSolution | None]:
    """`(key, solution)` from the ambient store. `solution` is `None` on a miss.

    The key comes back either way so the caller can store the answer under it
    once it has one, without hashing the section twice.
    """
    store = current()
    if store is None:
        return None, None
    key = key_for(section, refinement, growth)
    primitive = store.get(key)
    if primitive is None:
        return key, None
    store.record_for_verification(section, refinement, growth)
    return key, primitive.solution(section)


def remember(key: bytes | None, solution: fields.FieldSolution) -> None:
    """Record a freshly solved cross-section, if there is a store to record it."""
    store = current()
    if store is not None and key is not None:
        store.put(key, Primitive.of(solution))


def solve(section: fields.CrossSection, refinement: float,
          growth: float = fields.DEFAULT_GROWTH) -> fields.FieldSolution:
    """`fields.solve`, served from the ambient cache when it can be.

    The in-process path. `crosstalk.SolveCache` does not use this — it has a
    pool to feed and needs the key before it dispatches — but everything with a
    plain solve in front of it does.
    """
    key, cached = lookup(section, refinement, growth)
    if cached is not None:
        return cached
    solution = fields.solve(section, refinement=refinement, growth=growth)
    remember(key, solution)
    return solution
