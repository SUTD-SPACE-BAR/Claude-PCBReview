"""The persistent solve cache.

Most of these are about one failure mode. A cache that is slow is a nuisance; a
cache that serves a *wrong* number is the worst thing this project could do,
because the number looks exactly like a measurement and nothing downstream can
tell. So the bulk of what follows asks the same question from different sides:
**can two different solve problems ever collide on one key?**
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from pcbreview import crosstalk, fields, routed, solvepool, solvestore


@pytest.fixture
def store(tmp_path):
    opened = solvestore.open_store(tmp_path / "solves.sqlite3")
    assert opened is not None
    yield opened
    opened.close()


def a_section(width_mm=0.25, height_mm=0.2, thickness_mm=0.035,
              epsilon_r=4.5, gap_to_pour_mm=0.3):
    return fields.microstrip_section(width_mm, height_mm, thickness_mm,
                                     epsilon_r, gap_to_pour_mm=gap_to_pour_mm,
                                     mask=fields.JLC_SOLDER_MASK)


# ---------------------------------------------------------------------------
# The key covers everything
# ---------------------------------------------------------------------------

def test_the_key_covers_every_field_of_every_dataclass_it_hashes():
    """The guard that makes the other tests here mean something.

    `canonical` walks `CrossSection`, `Slab` and `Conductor` field by field. If
    someone adds a field to one of them and not to `_KNOWN_SHAPE`, every
    cross-section differing only in that field would collide on one key and the
    cache would serve the wrong solve — silently, and for ever.

    So the shape is asserted here *and* checked at import, where a mismatch
    turns the cache off rather than letting it answer.
    """
    for name, expected in solvestore._KNOWN_SHAPE.items():
        actual = tuple(f.name for f in
                       dataclasses.fields(getattr(fields, name)))
        assert actual == expected, (
            f"fields.{name} has changed shape. Teach solvestore._KNOWN_SHAPE "
            f"and solvestore.canonical about {set(actual) - set(expected)} "
            f"before the cache is allowed to key on it.")
    assert solvestore.SHAPE_IS_KNOWN


def test_an_unknown_dataclass_shape_disables_the_cache(monkeypatch):
    monkeypatch.setattr(solvestore, "SHAPE_IS_KNOWN", False)
    assert solvestore.key_for(a_section(), 1.5) is None
    assert solvestore.open_store("ignored.sqlite3") is None


@pytest.mark.parametrize("changed", [
    {"width_mm": 0.26}, {"height_mm": 0.21}, {"thickness_mm": 0.036},
    {"epsilon_r": 4.6}, {"gap_to_pour_mm": 0.31},
])
def test_any_change_to_the_geometry_changes_the_key(changed):
    base = solvestore.key_for(a_section(), 1.5)
    assert solvestore.key_for(a_section(**changed), 1.5) != base


def test_refinement_and_growth_are_in_the_key():
    section = a_section()
    assert solvestore.key_for(section, 1.5) != solvestore.key_for(section, 3.0)
    assert (solvestore.key_for(section, 1.5, growth=1.25)
            != solvestore.key_for(section, 1.5, growth=1.30))


def test_the_solder_mask_is_in_the_key():
    """Not a parameter of `solve`, but it changes the slabs, which are hashed."""
    with_mask = fields.microstrip_section(0.25, 0.2, 0.035, 4.5,
                                          gap_to_pour_mm=0.3,
                                          mask=fields.JLC_SOLDER_MASK)
    without = fields.microstrip_section(0.25, 0.2, 0.035, 4.5,
                                        gap_to_pour_mm=0.3, mask=None)
    assert solvestore.key_for(with_mask, 1.5) != solvestore.key_for(without, 1.5)


def test_conductor_order_is_in_the_key():
    """`fields` treats conductor 0 as the victim, so order is not incidental."""
    section = fields.coupled_microstrip_section(0.2, 0.3, 0.2, 0.2, 0.035, 4.5)
    swapped = dataclasses.replace(
        section, conductors=list(reversed(section.conductors)))
    assert solvestore.key_for(section, 3.0) != solvestore.key_for(swapped, 3.0)


def test_the_solver_fingerprint_is_in_the_key(monkeypatch):
    before = solvestore.key_for(a_section(), 1.5)
    monkeypatch.setattr(solvestore, "_fingerprint_cache", "a-different-solver")
    assert solvestore.key_for(a_section(), 1.5) != before


def test_no_fingerprint_means_no_cache(monkeypatch, tmp_path):
    """A build that cannot read its own solver declines to cache rather than
    guess that the solver has not changed."""
    monkeypatch.setattr(solvestore, "_fingerprint_cache", None)
    assert solvestore.key_for(a_section(), 1.5) is None
    assert solvestore.open_store(tmp_path / "s.sqlite3") is None


def test_a_description_cannot_forge_a_field_boundary():
    """`!r` in the key, so a newline inside a string is not a separator."""
    one = dataclasses.replace(a_section(), description="a\nfeature_mm=0x1p-1")
    two = dataclasses.replace(a_section(), description="a")
    assert solvestore.key_for(one, 1.5) != solvestore.key_for(two, 1.5)


def test_the_sign_of_zero_survives_the_key():
    one = dataclasses.replace(a_section(), x_left=0.0)
    two = dataclasses.replace(a_section(), x_left=-0.0)
    assert solvestore.key_for(one, 1.5) != solvestore.key_for(two, 1.5)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_a_stored_solution_comes_back_bit_for_bit(store):
    section = a_section()
    solved = fields.solve(section, refinement=3.0)
    key = solvestore.key_for(section, 3.0)
    store.put(key, solvestore.Primitive.of(solved))

    restored = store.get(key).solution(section)
    assert np.array_equal(restored.capacitance, solved.capacitance)
    assert np.array_equal(restored.capacitance_air, solved.capacitance_air)
    assert restored.mesh_nodes == solved.mesh_nodes
    assert restored.warnings == solved.warnings
    # The derived numbers are what actually reach a finding.
    assert restored.z0_ohm == solved.z0_ohm
    assert restored.epsilon_eff == solved.epsilon_eff


def test_a_restored_matrix_is_writable(store):
    """`frombuffer` hands back a read-only view; numpy downstream wants one it
    can work with, and a copy is cheaper than finding out where."""
    section = a_section()
    key = solvestore.key_for(section, 3.0)
    store.put(key, solvestore.Primitive.of(fields.solve(section, refinement=3.0)))
    restored = store.get(key)
    restored.capacitance[0, 0] = 1.0            # must not raise


def test_a_two_conductor_section_round_trips(store):
    section = fields.coupled_microstrip_section(0.2, 0.2, 0.2, 0.2, 0.035, 4.5)
    solved = fields.solve(section, refinement=3.0)
    assert solved.capacitance.shape == (2, 2)
    key = solvestore.key_for(section, 3.0)
    store.put(key, solvestore.Primitive.of(solved))
    restored = store.get(key).solution(section)
    assert np.array_equal(restored.capacitance, solved.capacitance)
    # And the coefficients derived from it, which is what crosstalk uses.
    assert crosstalk.coefficients(restored) == crosstalk.coefficients(solved)


def test_a_blob_this_build_cannot_read_is_a_miss_not_a_crash(store):
    key = solvestore.key_for(a_section(), 3.0)
    store.put(key, solvestore.Primitive.of(fields.solve(a_section(),
                                                        refinement=3.0)))
    store.flush()
    store._connection.execute("UPDATE solves SET value = ?", (b"junk",))
    store._connection.commit()
    assert store.get(key) is None


def test_the_stored_row_contains_no_geometry(store):
    """The privacy property, asserted rather than asserted-in-a-docstring.

    Rows are `sha256 -> floats`. Nothing in the file can be read back into
    someone's trace widths or layer heights, which is what lets this cache be
    shared across projects when `review/.cache` cannot be.
    """
    section = a_section()
    key = solvestore.key_for(section, 3.0)
    store.put(key, solvestore.Primitive.of(fields.solve(section,
                                                        refinement=3.0)))
    store.flush()
    raw = b"".join(bytes(row[0]) + bytes(row[1]) for row in
                   store._connection.execute("SELECT key, value FROM solves"))
    assert section.description.encode() not in raw
    for conductor in section.conductors:
        assert conductor.name.encode() not in raw


# ---------------------------------------------------------------------------
# Serving a solve
# ---------------------------------------------------------------------------

def test_the_ambient_store_is_off_unless_attached():
    assert solvestore.current() is None
    assert solvestore.lookup(a_section(), 3.0) == (None, None)


def test_attached_restores_the_previous_store(store):
    with solvestore.attached(store):
        assert solvestore.current() is store
        with solvestore.attached(None, enabled=False):
            assert solvestore.current() is None
        assert solvestore.current() is store
    assert solvestore.current() is None


def test_solve_serves_the_second_call_from_disk(store):
    section = a_section()
    with solvestore.attached(store):
        first = solvestore.solve(section, 3.0)
        assert store.writes == 1
        second = solvestore.solve(section, 3.0)
    assert store.hits == 1
    assert np.array_equal(first.capacitance, second.capacitance)
    assert first.z0_ohm == second.z0_ohm


def test_a_store_survives_being_closed_and_reopened(tmp_path):
    path = tmp_path / "solves.sqlite3"
    section = a_section()
    first = solvestore.open_store(path)
    with solvestore.attached(first):
        solved = solvestore.solve(section, 3.0)
    first.close()

    second = solvestore.open_store(path)
    with solvestore.attached(second):
        restored = solvestore.solve(section, 3.0)
    assert second.hits == 1
    assert np.array_equal(restored.capacitance, solved.capacitance)
    second.close()


def test_verify_recomputes_and_matches(store):
    section = a_section()
    with solvestore.attached(store):
        solvestore.solve(section, 3.0)
    result = store.verify([section], 3.0)
    assert result == {"checked": 1, "matched": 1, "absent": 0, "mismatched": []}


def test_verify_catches_a_corrupted_value(store):
    section = a_section()
    with solvestore.attached(store):
        solvestore.solve(section, 3.0)
    store.flush()
    wrong = solvestore.Primitive(
        capacitance=np.array([[1.0]]), capacitance_air=np.array([[2.0]]),
        mesh_nodes=7)
    store._connection.execute("UPDATE solves SET value = ?",
                              (wrong.to_blob(),))
    store._connection.commit()
    result = store.verify([section], 3.0)
    assert result["matched"] == 0
    assert result["mismatched"] == [section.description]


def test_verify_reports_a_section_it_has_never_seen(store):
    assert store.verify([a_section()], 3.0)["absent"] == 1


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

def test_the_table_is_pruned_to_its_limit(store):
    for index in range(40):
        store.put(bytes([index]) * 32,
                  solvestore.Primitive(np.array([[float(index)]]),
                                       np.array([[1.0]]), index))
    assert store.count() == 40
    store.prune(limit=10)
    assert store.count() == 10


def test_clear_empties_it(store):
    store.put(b"k" * 32, solvestore.Primitive(np.array([[1.0]]),
                                              np.array([[1.0]]), 1))
    assert store.count() == 1
    store.clear()
    assert store.count() == 0


def test_the_environment_can_switch_it_off(monkeypatch):
    monkeypatch.setenv(solvestore.CACHE_ENV, "off")
    assert solvestore.default_path() is None
    assert solvestore.open_default() is None


def test_the_environment_can_move_it(monkeypatch, tmp_path):
    monkeypatch.setenv(solvestore.CACHE_ENV, str(tmp_path))
    assert solvestore.default_path() == tmp_path / "solves.sqlite3"


def test_an_unopenable_store_is_none_not_an_exception(tmp_path):
    """A cache that will not open is a slow review, never a refused one."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    assert solvestore.open_store(blocker / "nested" / "s.sqlite3") is None


def test_for_audit_three_states(store, tmp_path):
    with solvestore.for_audit(False) as resolved:
        assert resolved is None and solvestore.current() is None
    with solvestore.for_audit(store) as resolved:
        assert resolved is store
    with solvestore.for_audit(tmp_path / "own.sqlite3") as resolved:
        assert resolved is not None and resolved is not store
    with solvestore.attached(store):
        with solvestore.for_audit(None) as resolved:
            assert resolved is store, "None must defer to an attached store"


# ---------------------------------------------------------------------------
# The two caches in front of it
# ---------------------------------------------------------------------------

def test_the_crosstalk_cache_reuses_a_stored_solve_and_starts_no_pool(store):
    """The ordering that matters: a board whose copper has not moved must not
    pay three seconds to spawn workers it will never give work to."""
    context = routed.LayerContext(
        reference_layer="In1.Cu", height_mm=0.2, epsilon_r=4.5,
        is_outer=True, thickness_mm=0.035)

    first = crosstalk.SolveCache(refinement=6.0)
    with solvestore.attached(store):
        first.coefficients(context, 0.2, 0.2, 0.2, 0.3)
        assert (first.solves, first.reused) == (1, 0)

        second = crosstalk.SolveCache(refinement=6.0)
        # A factory that would fail the test if it were ever called.
        second.pool_factory = lambda: pytest.fail("started a pool on a warm cache")
        second.POOL_THRESHOLD = 1
        landed = second.coefficients(context, 0.2, 0.2, 0.2, 0.3)

    assert (second.solves, second.reused) == (0, 1)
    assert landed == first.coefficients(context, 0.2, 0.2, 0.2, 0.3)


def test_hit_rate_still_measures_the_in_memory_cache(store):
    """Not silently repurposed into a report on the persistent store."""
    context = routed.LayerContext(
        reference_layer="In1.Cu", height_mm=0.2, epsilon_r=4.5,
        is_outer=True, thickness_mm=0.035)
    cache = crosstalk.SolveCache(refinement=6.0)
    with solvestore.attached(store):
        for _ in range(4):
            cache.coefficients(context, 0.2, 0.2, 0.2, 0.3)
    assert cache.lookups == 4
    assert cache.distinct == 1
    assert cache.hit_rate == pytest.approx(0.75)


def test_the_pool_returns_matrices_that_rebuild_the_solution():
    """What crosses the process boundary, checked without a process.

    `solve_matrices` is the worker's whole contract: the parent rebuilds a
    `FieldSolution` from what comes back and reduces it. If that rebuild were
    not exact, a pooled review and an in-process one would disagree.
    """
    section = fields.coupled_microstrip_section(0.2, 0.2, 0.2, 0.2, 0.035, 4.5)
    capacitance, capacitance_air, nodes = solvepool.solve_matrices(section, 6.0)
    rebuilt = fields.solution_from(section, capacitance, capacitance_air, nodes)
    direct = fields.solve(section, refinement=6.0)
    assert crosstalk.coefficients(rebuilt) == crosstalk.coefficients(direct)


def test_a_worker_refuses_to_start_a_pool(monkeypatch):
    """The fork-bomb guard, HANDOFF 15A.2b."""
    import multiprocessing

    class NotMain:
        name = "SpawnPoolWorker-3"

    monkeypatch.setattr(multiprocessing, "current_process", lambda: NotMain())
    with pytest.raises(RuntimeError, match="15A.2b"):
        solvepool.SolvePool(workers=4)


def test_solution_from_and_solve_agree_on_warnings():
    """The reconstruction path and the solved path share one threshold."""
    section = a_section()
    solved = fields.solve(section, refinement=3.0)
    rebuilt = fields.solution_from(section, solved.capacitance,
                                   solved.capacitance_air, solved.mesh_nodes)
    assert rebuilt.warnings == solved.warnings
    loud = fields.solution_from(section, solved.capacitance,
                                solved.capacitance_air, 500_000)
    assert loud.warnings and "500000 nodes" in loud.warnings[0]


def test_reading_a_row_moves_its_place_in_the_queue(store):
    """LRU, and it is `used` that has to move — not just `made`.

    Pruning by insertion order would evict a cross-section reused on every
    review in favour of one stored once and never wanted again, which is the
    opposite of what a shared cache should keep.
    """
    keys = [bytes([n]) * 32 for n in range(4)]
    for key in keys:
        store.put(key, solvestore.Primitive(np.array([[1.0]]),
                                            np.array([[1.0]]), 1))
    store.flush()
    stamps_before = dict(store._connection.execute(
        "SELECT key, used FROM solves"))

    store.get(keys[0])                       # the oldest row, read again
    store.flush()
    stamps_after = dict(store._connection.execute(
        "SELECT key, used FROM solves"))

    assert stamps_after[keys[0]] >= stamps_before[keys[0]]
    assert stamps_after[keys[0]] == max(stamps_after.values())
    store.prune(limit=1)
    assert [bytes(row[0]) for row in
            store._connection.execute("SELECT key FROM solves")] == [keys[0]]
