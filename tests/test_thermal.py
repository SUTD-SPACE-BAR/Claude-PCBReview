"""Thermal manifest, junction temperature, and the coverage half.

The numbers in these fixtures are **synthetic round figures**, not transcribed
from any real datasheet. That is deliberate: no file in this repo should carry
a thermal value that could be mistaken for one a human actually read, which is
the whole premise of `thermal.py`.
"""

import math
from pathlib import Path

import pytest

from pcbreview import thermal


def _manifest(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "t.thermal.yaml"
    path.write_text(body)
    return path


GOOD = """
ambient_c: 25
parts:
  U1:
    part: EXAMPLE-1
    theta_ja_c_per_w: 50.0
    t_junction_max_c: 150.0
    dissipation_w: 1.0
    source: "synthetic test value, not a datasheet"
"""


# -- the physics ----------------------------------------------------------

def test_junction_temperature_is_ambient_plus_theta_times_power(tmp_path):
    """Tj = 25 + 50 x 1.0 = 75 C, with 75 C of headroom to a 150 C limit."""
    manifest = thermal.load(_manifest(tmp_path, GOOD))
    result, = thermal.assess(manifest)
    assert result.junction_c == pytest.approx(75.0)
    assert result.headroom_c == pytest.approx(75.0)
    # 50 C of rise out of the 125 C available between 25 C and 150 C.
    assert result.rise_fraction == pytest.approx(50.0 / 125.0)
    assert result.severity == "info"


def test_rise_is_normalised_against_available_headroom_not_the_limit(tmp_path):
    """A part in a hot bay has less room, and the same watts read worse.

    The same 1 W part rated to 150 C has 125 C to play with at 25 C ambient and
    only 50 C at 100 C — so the identical dissipation is 40% of the budget in
    one case and 100% of it in the other.
    """
    hot = GOOD.replace("ambient_c: 25", "ambient_c: 100")
    result, = thermal.assess(thermal.load(_manifest(tmp_path, hot)))
    assert result.junction_c == pytest.approx(150.0)
    assert result.headroom_c == pytest.approx(0.0)
    assert result.rise_fraction == pytest.approx(1.0)
    assert result.severity == "critical"


def test_per_part_ambient_overrides_the_global(tmp_path):
    body = GOOD + "    ambient_c: 85\n"
    result, = thermal.assess(thermal.load(_manifest(tmp_path, body)))
    assert result.ambient_c == pytest.approx(85.0)
    assert result.junction_c == pytest.approx(135.0)


def test_severity_bands(tmp_path):
    """60% / 80% / 100% of the available rise, which are judgement calls."""
    for dissipation, expected in ((0.5, "info"), (1.6, "minor"),
                                  (2.1, "major"), (2.6, "critical")):
        body = GOOD.replace("dissipation_w: 1.0",
                            f"dissipation_w: {dissipation}")
        result, = thermal.assess(thermal.load(_manifest(tmp_path, body)))
        assert result.severity == expected, (dissipation, result.rise_fraction)


def test_a_part_already_over_its_limit_at_idle_is_infinite_not_negative(tmp_path):
    """Tj(max) at or below ambient leaves no budget; the ratio must not go negative."""
    body = GOOD.replace("t_junction_max_c: 150.0", "t_junction_max_c: 25.0")
    result, = thermal.assess(thermal.load(_manifest(tmp_path, body)))
    assert math.isinf(result.rise_fraction)
    assert result.severity == "critical"


# -- the loader refuses rather than defaulting ----------------------------

@pytest.mark.parametrize("field", ["theta_ja_c_per_w", "t_junction_max_c",
                                   "dissipation_w", "source"])
def test_a_missing_field_is_an_error_not_a_default(tmp_path, field):
    """The silent-zero failure this whole design exists to prevent (§36).

    A part entered with no `dissipation_w` would read as 0 W and report a
    comfortable junction temperature — indistinguishable from a part that was
    measured and found cool.
    """
    body = "\n".join(line for line in GOOD.splitlines()
                     if not line.strip().startswith(field + ":"))
    with pytest.raises(thermal.ThermalError) as exc:
        thermal.load(_manifest(tmp_path, body))
    assert field in str(exc.value)


def test_a_missing_file_is_an_error(tmp_path):
    with pytest.raises(thermal.ThermalError):
        thermal.load(tmp_path / "nope.yaml")


def test_a_document_without_parts_is_not_a_manifest(tmp_path):
    with pytest.raises(thermal.ThermalError):
        thermal.load(_manifest(tmp_path, "ambient_c: 25\n"))


def test_source_is_carried_through_to_the_result(tmp_path):
    """An unattributed thermal number is indistinguishable from a guess."""
    manifest = thermal.load(_manifest(tmp_path, GOOD))
    assert manifest.parts["U1"].source == "synthetic test value, not a datasheet"


# -- the coverage half, which is the deliverable until data exists --------

class _FakeComponent:
    def __init__(self, reference, value, lib_id="", description=None, dnp=False):
        self.reference, self.value, self.lib_id = reference, value, lib_id
        self.description, self.dnp = description, dnp


class _FakeSchematic:
    def __init__(self, components):
        self.components = components


def test_dissipative_shortlist_finds_parts_named_by_class():
    schematic = _FakeSchematic([
        _FakeComponent("U1", "AMS1117-3.3", "Regulator_Linear:AMS1117",
                       "1A Low Dropout regulator"),
        _FakeComponent("Q1", "AO3400A", "Transistor_FET:AO3400A",
                       "30V Vds, N-Channel MOSFET"),
        _FakeComponent("R1", "10k", "Device:R", "Resistor"),
        _FakeComponent("C1", "100n", "Device:C", "Unpolarized capacitor"),
    ])
    assert thermal.dissipative_candidates(schematic) == [
        ("Q1", "AO3400A"), ("U1", "AMS1117-3.3")]


def test_shortlist_does_not_match_a_part_number_substring():
    """`RCLAMP3354S.TCT` contains "MP3354" and is an ESD clamp, not a converter.

    Found on the first run against the unreferenced four-layer board. The manufacturer-prefix
    patterns are only safe anchored at a word boundary.
    """
    schematic = _FakeSchematic([
        _FakeComponent("U11", "RCLAMP3354S.TCT", "Custom Symbols:RCLAMP3354S.TCT"),
    ])
    assert thermal.dissipative_candidates(schematic) == []


def test_shortlist_does_not_match_a_mechanical_pushbutton():
    """KiCad describes `SW_Push` as "Push button switch" — matched on four of
    the eight designs in this repo before `switch` was narrowed."""
    schematic = _FakeSchematic([
        _FakeComponent("SW1", "SW_Push", "Switch:SW_Push",
                       "Push button switch, generic, two pins"),
    ])
    assert thermal.dissipative_candidates(schematic) == []


def test_shortlist_does_not_match_a_power_rating():
    """A 0.1 W resistor and a 400 W TVS's *peak pulse* rating are not thermal."""
    schematic = _FakeSchematic([
        _FakeComponent("R7", "22R +-5% 0.1W", "Device:R", "Resistor"),
        _FakeComponent("D1", "SMAJ5.0A", "Diode:SMAJ5.0A",
                       "400W unidirectional Transient Voltage Suppressor"),
    ])
    assert thermal.dissipative_candidates(schematic) == []


def test_shortlist_finds_a_mosfet_described_only_parametrically():
    """a second-source 17 W MOSFET whose description
    never says "MOSFET"."""
    schematic = _FakeSchematic([
        _FakeComponent("Q8", "XYZ4321", "Custom Symbols:XYZ4321",
                       "Number:1 N-Channel Drain to Source Voltage:30V "
                       "Current - Continuous Drain(Id):8A RDS(on):13m "
                       "Pd - Power Dissipation:17W"),
    ])
    assert thermal.dissipative_candidates(schematic) == [("Q8", "XYZ4321")]


def test_shortlist_finds_the_board_processor():
    """§40: the list was built out of power parts only and missed every board's
    processor — the largest single heat source on five of the seven designs
    here. `Teensy_4.1` is the hard case: value `~`, no description, only a
    library id."""
    schematic = _FakeSchematic([
        _FakeComponent("U1", "ESP32-S3-WROOM-1", "RF_Module:ESP32-S3-WROOM-1",
                       "RF Module, ESP32-S3 SoC, Wi-Fi 802.11b/g/n, Bluetooth"),
        _FakeComponent("U2", "~", "the processor module Dev Boards:Teensy_4.1"),
        _FakeComponent("IC1", "MIMXRT1062DVJ6B", "MCU_NXP:MIMXRT1062DVJ6B",
                       "IC MCU 32BIT EXT MEM 196MAPBGA"),
    ])
    assert thermal.dissipative_candidates(schematic) == [
        ("IC1", "MIMXRT1062DVJ6B"), ("U1", "ESP32-S3-WROOM-1"),
        ("U2", "the processor module Dev Boards:Teensy_4.1")]


def test_the_processor_words_do_not_match_passives_or_connectors():
    """The counterpart zero. `soc` and `mcu` are anchored for the same reason
    the manufacturer prefixes are — an unanchored `soc` matches "socket"."""
    schematic = _FakeSchematic([
        _FakeComponent("J1", "Conn_01x04", "Connector:Conn_01x04",
                       "Generic connector socket, 4 pins"),
        _FakeComponent("C1", "100n", "Device:C", "Unpolarized capacitor"),
        _FakeComponent("R1", "10k", "Device:R", "Resistor"),
    ])
    assert thermal.dissipative_candidates(schematic) == []


def test_dnp_parts_are_not_asked_for():
    schematic = _FakeSchematic([
        _FakeComponent("U1", "AMS1117-3.3", "Regulator_Linear:AMS1117",
                       "regulator", dnp=True)])
    assert thermal.dissipative_candidates(schematic) == []


def test_coverage_distinguishes_no_data_from_no_parts():
    schematic = _FakeSchematic([
        _FakeComponent("U1", "AMS1117-3.3", "", "Low Dropout regulator")])
    empty = thermal.coverage(schematic, None)
    assert empty == {"manifest": False, "parts_with_data": 0,
                     "dissipative_candidates": 1,
                     "candidates_without_data": 1, "unclassified_parts": 0,
                     "unknown_references": []}


def test_coverage_names_a_manifest_entry_that_is_not_on_the_board(tmp_path):
    """A designator that does not exist is a transcription error, and the part
    it was meant for went unassessed."""
    schematic = _FakeSchematic([_FakeComponent("U2", "x", "", "")])
    manifest = thermal.load(_manifest(tmp_path, GOOD))
    assert thermal.coverage(schematic, manifest)["unknown_references"] == ["U1"]


def test_default_path_sits_beside_the_board():
    assert thermal.default_path("/x/Board.kicad_pcb").name == "Board.thermal.yaml"


# -- the check ------------------------------------------------------------

def test_check_reports_that_it_did_not_run_and_names_what_it_needs():
    from pcbreview.audit import check_thermal

    schematic = _FakeSchematic([
        _FakeComponent("U1", "AMS1117-3.3", "", "Low Dropout regulator"),
        _FakeComponent("R1", "10k", "Device:R", "Resistor")])
    finding, = check_thermal(schematic, None)
    assert finding.check == "thermal"
    assert finding.severity == "info"          # absence is not a defect
    assert "did not run" in finding.title
    assert finding.measurement["candidates"] == ["U1"]
    assert finding.intent_needed


def test_an_unknown_designator_gets_no_junction_temperature(tmp_path):
    """"U99 runs at 41 C" for a part that is not on the board reads as a
    measurement and is not one. Found by reading the first real run's output."""
    from pcbreview.audit import check_thermal

    schematic = _FakeSchematic([_FakeComponent("U2", "x", "", "")])
    findings = check_thermal(schematic, thermal.load(_manifest(tmp_path, GOOD)))
    assert not [f for f in findings if "runs at" in f.title]
    assert [f for f in findings if "not on this board" in f.title]


# -- §43: the H-bridge class, and the parts nothing can classify -----------

def test_shortlist_finds_a_motor_driver_named_only_by_its_part_number():
    """§43: the four-layer motor controller's eight TMI8260SP-MS H-bridges are 5 A each
    and the largest per-part dissipators on that board. Value and library id
    are both the bare part number and the description is empty, so `driver`,
    `h-bridge` and `motor` all had nothing to read and the front end was never
    asking for their datasheet."""
    schematic = _FakeSchematic([
        _FakeComponent("U9", "TMI8260SP-MS", "Custom:TMI8260SP-MS"),
    ])
    assert thermal.dissipative_candidates(schematic) == [("U9", "TMI8260SP-MS")]


def test_the_motor_driver_prefix_is_anchored_like_the_others():
    """The counterpart zero: unanchored, `tmi8` would match inside any longer
    token. This is the mistake `mp\\d{4}` inside `RCLAMP3354S` already made."""
    schematic = _FakeSchematic([
        _FakeComponent("U1", "SITMI8260X", "Custom:SITMI8260X"),
        _FakeComponent("C1", "100n", "Device:C", "Unpolarized capacitor"),
    ])
    assert thermal.dissipative_candidates(schematic) == []


def test_unclassified_parts_names_what_nothing_could_decide():
    """A semiconductor with no description and a bare part number is invisible
    to a text match — and indistinguishable from one that was examined and
    found not to dissipate. That silent zero is what §43 cost."""
    schematic = _FakeSchematic([
        _FakeComponent("U1", "AMS1117-3.3", "", "Low Dropout regulator"),
        _FakeComponent("U2", "MAX1562", "MIMXRT1062DVJ6B:MAX1562"),
        _FakeComponent("Q1", "~", "Custom:SSM6J214FE"),
        _FakeComponent("R1", "10k", "Device:R", "Resistor"),
        _FakeComponent("J1", "Conn_01x04", "Connector:Conn_01x04"),
    ])
    found = thermal.unclassified_parts(schematic)
    assert [(p.reference, p.label) for p in found] == [
        ("Q1", "Custom:SSM6J214FE"), ("U2", "MAX1562")]
    assert all("no description at all" in p.reason for p in found)
    references = {p.reference for p in found}
    # already classified, so not also reported as unknown
    assert "U1" not in references
    # a resistor and a connector are not semiconductors and are not asked about
    assert "R1" not in references
    assert "J1" not in references


def test_a_catalogue_number_is_not_a_description():
    """An order code is not a description of anything. Treating it as one
    counted four sensors and a transistor across two designs here as
    "described", so nothing ever asked what they were.

    The codes below are fabricated: the rule matches on the *shape* of the
    value, so a real one would identify a design and prove nothing extra.
    """
    schematic = _FakeSchematic([
        _FakeComponent("U13", "SENSOR-PART", "x:Sensor", "C1234567"),
        _FakeComponent("U20", "MEMORY-PART", "x:Memory", "C7654321"),
    ])
    found = thermal.unclassified_parts(schematic)
    assert [p.reference for p in found] == ["U13", "U20"]
    # the code is real — it is just filed in the wrong field, so it is still
    # the thing that lets the front end resolve the part without asking.
    assert [p.lcsc for p in found] == ["C1234567", "C7654321"]
    assert all("describes nothing" in p.reason for p in found)


def test_a_class_word_with_no_rating_cannot_tell_a_rectifier_from_a_signal_diode():
    """§53.11's open hole. Both of these describe themselves as a Schottky
    diode; one is a 3 A rectifier and one is a 200 mA signal part, and neither
    description says which."""
    schematic = _FakeSchematic([
        _FakeComponent("D2", "SS34", "Device:D_Schottky", "Schottky diode"),
        _FakeComponent("D20", "BAT54", "Device:D_Schottky", "Schottky diode"),
    ])
    found = thermal.unclassified_parts(schematic)
    assert [p.reference for p in found] == ["D2", "D20"]
    assert all("states no rating" in p.reason for p in found)
    assert all(p.lcsc is None for p in found)


def test_a_class_that_cannot_be_the_hot_part_needs_no_rating():
    """The counterpart zero, and the whole reason the rule above is narrow:
    without it these thirty-odd parts per project join the list and nobody
    reads it."""
    schematic = _FakeSchematic([
        _FakeComponent("D1", "LED", "Device:LED", "Light emitting diode"),
        _FakeComponent("D9", "ZENER-PART", "Device:D_Zener", "Zener diode"),
        _FakeComponent("U9", "ESD-PART", "x:Clamp",
                       "Very low capacitance ESD protection diode, 2 data-line"),
        _FakeComponent("U8", "LOGIC-PART", "x:Buffer",
                       "Dual Buffer, Low-Voltage CMOS"),
    ])
    assert thermal.unclassified_parts(schematic) == []


def test_a_description_that_states_a_rating_is_a_description():
    """The part is described well enough to judge, whether or not the class
    list happened to match a word in it."""
    schematic = _FakeSchematic([
        _FakeComponent("D3", "SS34", "Device:D",
                       "Diode Configuration:Single Current - Rectified:10A "
                       "Forward(Vf@If):340mV"),
    ])
    assert thermal.unclassified_parts(schematic) == []


def test_the_finding_splits_on_whether_the_part_can_be_looked_up():
    """the ruling, 2026-08-04: a part with an LCSC code can be resolved from
    the catalogue; a part without one needs the user before it can be reviewed
    at all. The front end acts on that split, so the finding has to make it."""
    from pcbreview.audit import check_thermal

    schematic = _FakeSchematic([
        _FakeComponent("U13", "SENSOR-PART", "x:Sensor", "C1234567"),
        _FakeComponent("D2", "SS34", "Device:D_Schottky", "Schottky diode"),
    ])
    finding, = check_thermal(schematic, None)
    text = " ".join(finding.detail)
    assert "C1234567" in text and "LCSC" in text
    assert "THE USER MUST SAY WHAT THESE PARTS ARE" in text
    detail = {d["reference"]: d for d in
              finding.measurement["unclassified_detail"]}
    assert detail["U13"]["lcsc"] == "C1234567"
    assert detail["D2"]["lcsc"] is None


def test_unclassified_parts_reach_the_finding_the_front_end_reads():
    from pcbreview.audit import check_thermal

    schematic = _FakeSchematic([
        _FakeComponent("U1", "AMS1117-3.3", "", "Low Dropout regulator"),
        _FakeComponent("U2", "MAX1562", "MIMXRT1062DVJ6B:MAX1562"),
    ])
    finding, = check_thermal(schematic, None)
    assert finding.measurement["unclassified"] == ["U2"]
    assert any("could not be classified" in line for line in finding.detail)


def test_dnp_parts_are_not_reported_as_unclassified():
    schematic = _FakeSchematic([
        _FakeComponent("U2", "MAX1562", "x:MAX1562", dnp=True)])
    assert thermal.unclassified_parts(schematic) == []


# -- §43a: a datasheet that states no theta_JA ----------------------------

UNSTATED = """
parts:
  U1:
    part: SOME-HBRIDGE
    theta_ja_stated: false
    t_junction_max_c: 150.0
    dissipation_w: 1.78
    source: "vendor spec p.3 — no theta_JA and no PD anywhere in the document"
"""


def test_a_datasheet_with_no_theta_ja_can_still_be_entered(tmp_path):
    """the ruling: a datasheet that was *supplied* must not fall out of
    the analysis because it happens not to quote a thermal resistance. Before
    this, the largest dissipator on a real board did exactly that."""
    manifest = thermal.load(_manifest(tmp_path, UNSTATED))
    part = manifest.parts["U1"]
    assert part.theta_ja_stated is False
    assert part.theta_ja_c_per_w is None
    assert part.rise_c is None
    assert part.junction_c(25.0) is None
    # (150 - 25) / 1.78
    assert part.required_theta_ja_c_per_w(25.0) == pytest.approx(70.22, abs=0.01)


def test_a_missing_theta_ja_is_refused_unless_it_is_declared_missing(tmp_path):
    """An omitted key cannot be told apart from a forgotten one, so it is an
    error — the same rule `stackup_cli transcribe` applies to `to_copper`."""
    text = UNSTATED.replace("    theta_ja_stated: false\n", "")
    with pytest.raises(thermal.ThermalError) as excinfo:
        thermal.load(_manifest(tmp_path, text))
    assert "theta_ja_stated" in str(excinfo.value)


def test_declaring_it_missing_and_also_giving_it_is_refused(tmp_path):
    text = UNSTATED.replace("    theta_ja_stated: false\n",
                            "    theta_ja_stated: false\n"
                            "    theta_ja_c_per_w: 70.0\n")
    with pytest.raises(thermal.ThermalError) as excinfo:
        thermal.load(_manifest(tmp_path, text))
    assert "one of the two is wrong" in str(excinfo.value)


def test_an_unassessable_part_is_not_a_pass(tmp_path):
    """It must not read as `info`, and it must not read as a temperature."""
    from pcbreview.audit import check_thermal

    schematic = _FakeSchematic([_FakeComponent("U1", "SOME-HBRIDGE", "x:HB")])
    manifest = thermal.load(_manifest(tmp_path, UNSTATED))
    finding = [f for f in check_thermal(schematic, manifest)
               if f.measurement.get("reference") == "U1"][0]
    assert finding.severity == "minor"
    assert "no theta_JA" in finding.title
    assert "70 C/W" in finding.title
    assert finding.measurement["junction_c"] is None
    assert finding.measurement["theta_ja_stated"] is False
    assert finding.measurement["required_theta_ja_c_per_w"] == pytest.approx(70.2)
    assert finding.intent_needed


def test_unassessable_parts_rank_above_the_ones_that_pass(tmp_path):
    """They are the open question, not the good news."""
    text = UNSTATED + """
  U2:
    part: COOL-LDO
    theta_ja_c_per_w: 50.0
    t_junction_max_c: 150.0
    dissipation_w: 0.1
    source: "vendor spec p.1"
"""
    results = thermal.assess(thermal.load(_manifest(tmp_path, text)))
    assert [r.part.reference for r in results] == ["U1", "U2"]
    assert results[0].assessed is False and results[1].assessed is True


# -- the negligible-dissipation floor (§51) -------------------------------

NEGLIGIBLE = """
ambient_c: 25
parts:
  U5:
    part: EXAMPLE-SUPERVISOR
    theta_ja_stated: false
    t_junction_max_c: 150.0
    dissipation_w: 0.0001
    source: "synthetic test value, not a datasheet"
"""


def test_a_part_below_the_floor_still_assesses_as_unquantifiable(tmp_path):
    """The floor lives in the *audit's* presentation, not in the physics: the
    assessment is unchanged and honest, and the required theta_JA is still
    computed. What changes is that a 0.1 mW part does not get ranked above a
    real junction temperature for it."""
    manifest = thermal.load(_manifest(tmp_path, NEGLIGIBLE))
    result, = thermal.assess(manifest)
    assert not result.assessed
    assert result.required_theta_ja_c_per_w > 1e6
    assert result.part.dissipation_w < thermal.NEGLIGIBLE_DISSIPATION_W


def test_the_floor_is_where_a_rise_stops_being_resolvable():
    """10 mW at 100 C/W is 1 C. There is no package and no board on which a
    part below the floor gets hot, so a missing theta_JA decides nothing."""
    assert thermal.NEGLIGIBLE_DISSIPATION_W == 0.010
    assert thermal.NEGLIGIBLE_DISSIPATION_W * 100.0 <= 1.0


def test_a_part_above_the_floor_is_not_swept_up_by_it(tmp_path):
    """The counterpart zero: 0.5 W with no theta_JA is exactly the case the
    unquantifiable branch exists for and must keep reporting."""
    manifest = thermal.load(_manifest(
        tmp_path, NEGLIGIBLE.replace("dissipation_w: 0.0001",
                                     "dissipation_w: 0.5")))
    result, = thermal.assess(manifest)
    assert result.part.dissipation_w > thermal.NEGLIGIBLE_DISSIPATION_W
    assert not result.assessed


# -- PD on a named board, and a missing Tj max (§52) -----------------------

PD_ON_A_BOARD = """
ambient_c: 25
parts:
  U12:
    part: EXAMPLE-LOAD-SWITCH
    pd_w: 2.4
    pd_at_tj_c: 150
    pd_board_note: "76.2 x 114.3 x 1.6 mm 4-layer, datasheet p.7"
    t_junction_max_c: 150.0
    dissipation_w: 0.70
    source: "synthetic test value, not a datasheet"
"""


def test_a_pd_figure_on_a_named_board_becomes_a_usable_theta_ja(tmp_path):
    """The schema used to force a binary: hand it theta_JA, or declare it
    absent. A datasheet quoting `PD = 2.4 W at Tj = 150 C` on a named board
    carries the thermal resistance exactly — (150-25)/2.4 = 52.1 C/W on that
    board — and declaring it absent gave the *less* informative answer."""
    manifest = thermal.load(_manifest(tmp_path, PD_ON_A_BOARD))
    result, = thermal.assess(manifest)
    assert result.part.theta_ja_derived_from_pd == pytest.approx(52.083, abs=1e-3)
    assert result.part.theta_ja_stated
    assert result.assessed
    assert result.summary()["theta_ja_source"] == "derived from PD on a named board"


def test_a_pd_figure_without_its_board_is_refused(tmp_path):
    """The same rule `source` enforces: a number whose conditions are not
    recorded cannot be checked."""
    with pytest.raises(thermal.ThermalError) as excinfo:
        thermal.load(_manifest(tmp_path, PD_ON_A_BOARD.replace(
            '    pd_board_note: "76.2 x 114.3 x 1.6 mm 4-layer, datasheet p.7"\n',
            "")))
    assert "pd_board_note" in str(excinfo.value)


def test_a_pd_figure_without_its_junction_temperature_is_refused(tmp_path):
    with pytest.raises(thermal.ThermalError) as excinfo:
        thermal.load(_manifest(tmp_path, PD_ON_A_BOARD.replace(
            "    pd_at_tj_c: 150\n", "")))
    assert "pd_at_tj_c" in str(excinfo.value)


def test_a_stated_theta_ja_still_wins_over_a_derived_one(tmp_path):
    manifest = thermal.load(_manifest(tmp_path, PD_ON_A_BOARD.replace(
        "    pd_w: 2.4", "    theta_ja_c_per_w: 40.0\n    pd_w: 2.4")))
    result, = thermal.assess(manifest)
    assert result.part.effective_theta_ja_c_per_w == 40.0
    assert result.summary()["theta_ja_source"] == "stated"


def test_an_unstated_tj_max_and_an_ambient_limit_are_recorded(tmp_path):
    """Some datasheets state no Tj max at all — only Tstg and an operating
    ambient range — and for those the ambient limit is the binding one."""
    manifest = thermal.load(_manifest(tmp_path, """
ambient_c: 25
parts:
  U13:
    part: EXAMPLE-DRIVER
    t_junction_max_stated: false
    t_junction_max_c: 150.0
    t_ambient_max_c: 85.0
    theta_ja_stated: false
    dissipation_w: 0.40
    source: "synthetic test value, not a datasheet"
"""))
    result, = thermal.assess(manifest)
    assert result.part.t_junction_max_stated is False
    assert result.part.t_ambient_max_c == 85.0
    assert result.summary()["t_junction_max_stated"] is False
    assert result.summary()["t_ambient_max_c"] == 85.0
