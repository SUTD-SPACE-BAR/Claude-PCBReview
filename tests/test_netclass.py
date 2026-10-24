"""Tests for Gate 2 — what a net is for, and therefore how much it matters.

the user's specification, 2026-08-03: infer whether a net is a communication bus
(uart, i2c, spi, rf, can, usb, pwm), care a lot about those and about power,
and audit everything else at low severity.

**Amended by him the same day (§49.3), and the amendment reverses the second
half of that.** An *unidentified* net is no longer audited at low severity, on
the measurement that unidentified was the only thing `other` ever contained:

> we cannot rely completely on pin types of the schematic symbols [...] When in
> doubt, treat the pin and hence the net as high priority.

So the demotion tests below pin the surviving rule — only the user's own
net-intent declaration lowers anything — and the classification tests are
unchanged, because the classification itself still gates the power-scoped
checks.

Four things are pinned here because they were wrong on the first run or would
be catastrophic if they regressed:

* `test_an_enable_pin_is_not_pwm` — `EN` was in the PWM pattern and classified
  a regulator enable, a `/VIN` net and a radio's enable as PWM on four designs.
* `test_a_part_finding_is_never_capped` — `heat_path` names the tab's net so
  the renderer can draw it, and capping on that basis demoted a 3.84 W part.
* `test_nothing_is_ever_promoted` — this gate exists to stop noise drowning
  signal, not to invent urgency.
* `test_an_unidentified_net_keeps_the_severity_its_check_computed` — the
  reversal itself, and the thing a future session is most likely to undo by
  reading §45.1 without §49.3.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview import netclass
from pcbreview.findings import Finding


class _Pin:
    def __init__(self, reference, number, name):
        self.reference, self.number, self.name = reference, str(number), name


class _Net:
    def __init__(self, name, pins=()):
        self.name = name
        self.pins = list(pins)


class _Schematic:
    def __init__(self, nets):
        self.nets = nets


def _classify(nets, rails=frozenset(), grounds=frozenset()):
    return netclass.classify(_Schematic(nets), set(rails), set(grounds))


# -- pin names, which are the vendor's words ------------------------------

@pytest.mark.parametrize("pin_name,kind", [
    ("SDA", "i2c"), ("SCL", "i2c"),
    ("MOSI", "spi"), ("MISO", "spi"), ("SCK", "spi"), ("CSB", "spi"),
    ("TXD0", "uart"), ("RXD", "uart"), ("CTS", "uart"),
    ("CANH", "can"), ("CAN_L", "can"),
    ("D+", "usb"), ("USB_D-", "usb"),
    ("RF_IN", "rf"), ("ANT", "rf"),
    ("PWM3", "pwm"), ("LED12", "pwm"), ("AIN1", "pwm"),
    ("XIN", "clock"), ("MCLK", "clock"),
])
def test_a_vendor_pin_name_classifies_the_net(pin_name, kind):
    classes = _classify([_Net("/whatever", [_Pin("U1", 3, pin_name)])])
    entry = classes.of("/whatever")
    assert entry.net_class == "protocol"
    assert entry.kind == kind
    assert entry.confidence == 0.9
    assert "manufacturer's symbol" in entry.evidence


def test_an_enable_pin_is_not_pwm():
    """`EN` was in the PWM pattern on the first run. A regulator enable is a DC
    control line, and this misclassified `/VIN`, `/EN` and a radio's enable
    across four designs."""
    classes = _classify([_Net("/EN", [_Pin("U1", 3, "EN")])])
    assert classes.of("/EN").net_class == "other"


@pytest.mark.parametrize("pin_name", ["RXEN", "CSENSE", "DMA", "VDDIO",
                                      "GPIO4", "NRESET", "SENSE"])
def test_pin_patterns_are_anchored(pin_name):
    """Unanchored, `RX` matches `RXEN` and `CS` matches `CSENSE`. Dropping the
    anchors roughly triples the protocol count and every addition is wrong."""
    classes = _classify([_Net("/x", [_Pin("U1", 1, pin_name)])])
    assert classes.of("/x").net_class == "other"


# -- net names, the fallback ----------------------------------------------

def test_the_net_name_is_used_only_when_no_pin_says_anything():
    classes = _classify([_Net("/CAN_TX", [_Pin("U1", 1, "IO7")])])
    entry = classes.of("/CAN_TX")
    assert entry.net_class == "protocol" and entry.kind == "can"
    assert entry.confidence == 0.5
    assert entry.evidence.startswith("net name")


def test_a_pin_name_beats_a_net_name():
    """The designer called it one thing and the part vendor another; the
    vendor wins, and the confidence records that it was the vendor."""
    classes = _classify([_Net("/SPI2 MOSI", [_Pin("U2", 14, "SDA")])])
    entry = classes.of("/SPI2 MOSI")
    assert entry.kind == "i2c" and entry.confidence == 0.9


# -- power ----------------------------------------------------------------

def test_rails_and_grounds_are_power_at_full_confidence():
    classes = _classify([_Net("/sig")], rails={"+3V3"}, grounds={"GND"})
    assert classes.of("+3V3").net_class == "power"
    assert classes.of("+3V3").kind == "supply"
    assert classes.of("GND").kind == "ground"
    assert classes.of("GND").confidence == 1.0
    assert classes.full_severity("GND") and classes.full_severity("+3V3")


def test_a_board_net_absent_from_the_schematic_is_other_not_missing():
    """A net the netlist does not carry still gets a class, at low confidence,
    so `apply` never has to guess."""
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"Net-(J1-Pad4)"})
    entry = classes.of("Net-(J1-Pad4)")
    assert entry.net_class == "other" and entry.confidence == 0.5


# -- the demotion rule ----------------------------------------------------
#
# The inferred cap is gone (§49.3). What these pin is the *new* rule: nothing
# lowers a severity except the user's own net-intent declaration.

def _finding(check, severity, nets):
    return Finding(check=check, title="t", severity=severity, score=5.0,
                   nets=list(nets))


def test_a_bus_net_keeps_its_severity():
    classes = _classify([_Net("/SDA", [_Pin("U1", 1, "SDA")])])
    findings = [_finding("return_path", "major", ["/SDA"])]
    result = netclass.apply(findings, classes)
    assert findings[0].severity == "major"
    assert result == {"capped": 0, "kept_at_full_severity": 1}


def test_an_unidentified_net_keeps_the_severity_its_check_computed():
    """The §49.3 ruling, and the reversal of what this module used to do.

    A plain GPIO used to cap at `minor` because nothing identified it. Measured
    across four boards, *every* `other` net was there for that reason — the
    classifier never positively identifies a net as low-stakes — so the cap was
    entirely an inference from absence of evidence, at `confidence=1.0`. It
    fires on custom symbols that under-name their pins, which is common, and
    The user ruled: when in doubt, high priority.
    """
    classes = _classify([_Net("/GPIO4", [_Pin("U1", 1, "GPIO4")])])
    assert classes.of("/GPIO4").net_class == "other"
    findings = [_finding("return_path", "major", ["/GPIO4"])]
    result = netclass.apply(findings, classes)
    assert findings[0].severity == "major"
    assert "severity_before_netclass" not in findings[0].measurement
    assert findings[0].detail == []
    assert result == {"capped": 0, "kept_at_full_severity": 1}


def test_only_the_users_own_declaration_lowers_a_severity(tmp_path):
    """Demotion is now the manifest's job alone, and it says whose word it was."""
    intent = _intent(tmp_path, """
nets:
  - match: "/TP*"
    net_class: other
""")
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/TP7"}, intent=intent)
    findings = [_finding("return_path", "major", ["/TP7"])]
    result = netclass.apply(findings, classes)
    assert findings[0].severity == netclass.OTHER_SEVERITY_CAP
    assert findings[0].measurement["severity_before_netclass"] == "major"
    assert any("you declared" in line for line in findings[0].detail)
    assert result == {"capped": 1, "kept_at_full_severity": 0}


def test_a_finding_touching_one_bus_net_is_not_capped():
    """Crosstalk names an aggressor and a victim. If either is a bus, the
    finding is about a bus."""
    classes = _classify([_Net("/SDA", [_Pin("U1", 1, "SDA")]),
                         _Net("/GPIO4", [_Pin("U1", 2, "GPIO4")])])
    findings = [_finding("crosstalk", "major", ["/GPIO4", "/SDA"])]
    netclass.apply(findings, classes)
    assert findings[0].severity == "major"


def test_one_undeclared_net_on_a_finding_is_enough_to_keep_it(tmp_path):
    """The counterpart to the rule above, now that demotion is user-driven: a
    crosstalk finding between a net the user waved off and one they said
    nothing about is still a finding about the second one."""
    intent = _intent(tmp_path, """
nets:
  - match: "/TP*"
    net_class: other
""")
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/TP7", "/GPIO4"}, intent=intent)
    findings = [_finding("crosstalk", "major", ["/TP7", "/GPIO4"])]
    netclass.apply(findings, classes)
    assert findings[0].severity == "major"


def test_a_part_finding_is_never_capped(tmp_path):
    """`heat_path` names the tab's net so the renderer can draw it. Capping on
    that basis demoted a 3.84 W part to `minor` on the first run.

    The net here is *declared* unimportant, which since §49.3 is the only thing
    that demotes anything — without that this test would pass on a code path
    that no longer exists.
    """
    intent = _intent(tmp_path, 'nets:\n  - match: "Net-(Q1-*)"\n'
                               '    net_class: other\n')
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"Net-(Q1-D)"}, intent=intent)
    assert classes.of("Net-(Q1-D)").demoted_by_user
    findings = [_finding("heat_path", "major", ["Net-(Q1-D)"]),
                _finding("thermal", "critical", ["Net-(Q1-D)"])]
    netclass.apply(findings, classes)
    assert [f.severity for f in findings] == ["major", "critical"]


def test_a_finding_with_no_nets_is_untouched():
    classes = _classify([])
    findings = [_finding("stackup_mismatch", "major", [])]
    netclass.apply(findings, classes)
    assert findings[0].severity == "major"


def test_nothing_is_ever_promoted():
    """An `info` finding on a bus stays `info`. This gate lowers noise; it does
    not invent urgency."""
    classes = _classify([_Net("/SDA", [_Pin("U1", 1, "SDA")])])
    findings = [_finding("return_path", "info", ["/SDA"])]
    netclass.apply(findings, classes)
    assert findings[0].severity == "info"


def test_an_already_minor_finding_is_not_annotated_twice(tmp_path):
    intent = _intent(tmp_path, 'nets:\n  - match: "/TP*"\n'
                               '    net_class: other\n')
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/TP7"}, intent=intent)
    findings = [_finding("return_path", "minor", ["/TP7"]),
                _finding("return_path", "info", ["/TP7"])]
    result = netclass.apply(findings, classes)
    assert result["capped"] == 0
    assert all(not f.detail for f in findings)


def test_the_census_makes_a_zero_readable():
    classes = _classify([_Net("/SDA", [_Pin("U1", 1, "SDA")]),
                         _Net("/GPIO4", [_Pin("U1", 2, "GPIO4")])],
                        grounds={"GND"})
    census = classes.census()
    assert census["by_class"] == {"power": 1, "protocol": 1, "other": 1}
    assert census["protocol_kinds"] == {"i2c": 1}
    assert census["from_pin_names"] == 1


@pytest.mark.parametrize("net_name,kind", [
    ("/SERVO5", "pwm"), ("/PWM3", "pwm"), ("/MOTOR2", "pwm"),
    ("/CAN_TX", "can"), ("/I2C1 SCL", "i2c"), ("/SPI2 MOSI", "spi"),
    ("/UART2 TX", "uart"), ("/USB2 DP", "usb"),
])
def test_a_bus_word_survives_a_separator_and_a_trailing_index(net_name, kind):
    """`_` and a trailing digit are both word characters, so `\\bSERVO\\b` misses
    `SERVO5` and `\\bCAN\\b` misses `CAN_TX`. Eight servo PWM channels on a real
    board were classified `other` and capped because of the second one."""
    classes = _classify([_Net(net_name, [_Pin("U1", 1, "IO7")])])
    entry = classes.of(net_name)
    assert entry.net_class == "protocol" and entry.kind == kind


def test_a_plain_gpio_is_still_other():
    """The counterpart zero — the digit tolerance must not swallow everything."""
    for name in ("/GPIO18", "/FIRE_CH4", "/LED_STAT", "/SENSE3"):
        classes = _classify([_Net(name, [_Pin("U1", 1, "IO7")])])
        assert classes.of(name).net_class == "other", name


@pytest.mark.parametrize("check", ["kelvin_sense", "crystal_loop",
                                   "decoupling_loop", "regulator_feedback",
                                   "esd_position", "heat_path", "thermal"])
def test_every_part_check_is_exempt(check, tmp_path):
    """These all name a net for the renderer and none of them is a statement
    about what the net carries. `kelvin_sense` was missing from the first
    version and seven findings on one board were demoted because of it.

    Declared unimportant, so the exemption is tested against the one code path
    that still demotes (§49.3)."""
    intent = _intent(tmp_path, 'nets:\n  - match: "/SENSE"\n'
                               '    net_class: other\n')
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/SENSE"}, intent=intent)
    findings = [_finding(check, "major", ["/SENSE"])]
    netclass.apply(findings, classes)
    assert findings[0].severity == "major"


# -- §46: declared intent, and width as power evidence --------------------

class _Track:
    def __init__(self, net, width_mm, length_mm, layer="F.Cu"):
        self.net, self.width_mm, self.length_mm, self.layer = (
            net, width_mm, length_mm, layer)


class _WidthBoard:
    def __init__(self, tracks):
        self.tracks = tracks
        self.copper_layers = ["F.Cu", "B.Cu"]

    def net(self, code):
        class _N:
            name = code
        return _N()


def _intent(tmp_path, text):
    path = tmp_path / "n.yaml"
    path.write_text(text)
    return netclass.load_intent(path)


def test_a_widened_track_is_treated_as_power():
    """The brief: a track above 0.2 mm is probably carrying current. A designer
    widens a track for exactly one reason."""
    board = _WidthBoard([_Track("/MOTOR_A", 0.8, 40.0)])
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/MOTOR_A"}, board=board)
    entry = classes.of("/MOTOR_A")
    assert entry.net_class == "power" and entry.kind == "wide"
    assert classes.full_severity("/MOTOR_A")


def test_one_fat_stub_does_not_make_a_signal_net_power():
    """The counterpart zero: it has to be most of the net."""
    board = _WidthBoard([_Track("/SIG", 0.8, 2.0), _Track("/SIG", 0.15, 40.0)])
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/SIG"}, board=board)
    assert classes.of("/SIG").net_class == "other"


def test_the_board_median_width_is_reported_so_the_rule_can_be_read():
    """A board whose default routing is already above the threshold has most
    of itself reclassified, and that must be visible rather than silent."""
    board = _WidthBoard([_Track("/A", 0.267, 50.0), _Track("/B", 0.267, 50.0)])
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/A", "/B"}, board=board)
    census = classes.census()
    assert census["median_routed_width_mm"] == pytest.approx(0.267)
    assert census["from_routed_width"] == 2


def test_a_declaration_beats_every_inference(tmp_path):
    """The firing-channel case: a power output that no power symbol
    drives, so nothing structural calls it one."""
    intent = _intent(tmp_path, """
nets:
  - match: "/FIRE_CH*"
    net_class: power
    note: "firing channel, 5 A into an e-match"
""")
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/FIRE_CH4"}, intent=intent)
    entry = classes.of("/FIRE_CH4")
    assert entry.net_class == "power" and entry.confidence == 1.0
    assert "declared by the user" in entry.evidence
    assert "e-match" in entry.evidence
    assert classes.declared == ["/FIRE_CH4"]


def test_a_declaration_can_also_demote(tmp_path):
    """Intent runs both ways — a spare bus the designer does not care about.

    Load-bearing since §49.3: demotion is now the *only* way a net gets
    quietened, so this path overrides even a vendor pin name."""
    intent = _intent(tmp_path, """
nets:
  - match: "/SPARE*"
    net_class: other
""")
    classes = netclass.classify(
        _Schematic([_Net("/SPARE_SDA", [_Pin("U1", 1, "SDA")])]),
        set(), set(), intent=intent)
    entry = classes.of("/SPARE_SDA")
    assert entry.net_class == "other" and entry.demoted_by_user
    findings = [_finding("return_path", "major", ["/SPARE_SDA"])]
    netclass.apply(findings, classes)
    assert findings[0].severity == netclass.OTHER_SEVERITY_CAP


def test_a_pattern_matching_nothing_is_reported(tmp_path):
    """A typo would otherwise leave the user believing a net is protected."""
    intent = _intent(tmp_path, """
nets:
  - match: "/PYR0*"
    net_class: power
    note: "typo"
""")
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/FIRE_CH4"}, intent=intent)
    assert classes.unmatched_declarations == ["/PYR0*"]
    assert classes.census()["declarations_matching_nothing"] == ["/PYR0*"]


def test_declaring_a_net_power_needs_no_justification(tmp_path):
    """The user may assert what their own net is for, without explaining it.

    This inverts an earlier rule that refused a `power` declaration carrying no
    `note`, on the grounds that it "exempts it from capping". **It exempts
    nothing.** The inferred cap was removed on 2026-08-03 — the module
    docstring and the four-board measurement behind it — and since then the
    only declaration that lowers a severity is `net_class: other`. `power` and
    `protocol` *admit* a net to `check_current_capacity` and `check_heat_path`,
    which is more scrutiny.

    So the rule demanded a justification for asking the engine to look harder,
    while the one declaration that quietens a finding needed none. The user,
    refused a plain `+3.3V is power`: *"dont try to assume the user must
    explain why they assert this or that."*
    """
    intent = _intent(tmp_path, 'nets:\n  - match: "/X"\n    net_class: power\n')
    assert intent.declarations[0].net_class == "power"
    assert intent.declarations[0].note is None


def test_a_note_is_still_carried_when_it_is_given(tmp_path):
    """Optional is not ignored — it is how a report says why."""
    intent = _intent(tmp_path, 'nets:\n  - match: "/X"\n    net_class: power\n'
                               '    note: "firing channel, 5 A for 100 ms"\n')
    assert intent.declarations[0].note == "firing channel, 5 A for 100 ms"


def test_demoting_a_net_needs_no_justification_either(tmp_path):
    """`other` never required a note, and that asymmetry was the tell."""
    intent = _intent(tmp_path, 'nets:\n  - match: "/X"\n    net_class: other\n')
    assert intent.declarations[0].net_class == "other"


def test_an_invalid_class_is_refused(tmp_path):
    with pytest.raises(netclass.NetIntentError) as excinfo:
        _intent(tmp_path, 'nets:\n  - match: "/X"\n    net_class: urgent\n')
    assert "protocol, power, other" in str(excinfo.value)


def test_the_first_matching_declaration_wins(tmp_path):
    intent = _intent(tmp_path, """
nets:
  - match: "/FIRE_CH1"
    net_class: other
  - match: "/FIRE_CH*"
    net_class: power
    note: "the rest of them"
""")
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/FIRE_CH1", "/FIRE_CH2"}, intent=intent)
    assert classes.of("/FIRE_CH1").net_class == "other"
    assert classes.of("/FIRE_CH2").net_class == "power"


# -- confirming the user's file was read (§51) ----------------------------

def test_a_loaded_manifest_confirms_itself_even_when_it_changed_nothing(tmp_path):
    """Silence was the only signal. A session wrote four declarations, the
    audit said `0 finding(s) lowered`, and then re-proposed all four nets as
    unidentified — so the output actively suggested the file had been ignored
    when every pattern had matched. They established otherwise by calling
    `load_intent` by hand."""
    intent = _intent(tmp_path, 'nets:\n  - match: "/TP*"\n'
                               '    net_class: other\n')
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/TP1", "/TP2"}, intent=intent)
    line = netclass.declaration_report(classes, {"capped": 0}, tmp_path / "n.yaml")
    assert line is not None
    assert "2 net(s) matched" in line
    assert "0 pattern(s) matched nothing" in line
    assert "already at or below minor" in line


def test_the_confirmation_counts_nets_and_patterns_separately(tmp_path):
    """Two patterns matching four nets is not "six declarations" — adding the
    two together was the first version's arithmetic and it was wrong."""
    intent = _intent(tmp_path, 'nets:\n  - match: "/TP*"\n'
                               '    net_class: other\n'
                               '  - match: "/NOSUCH*"\n'
                               '    net_class: other\n')
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/TP1", "/TP2"}, intent=intent)
    line = netclass.declaration_report(classes, {"capped": 1})
    assert "2 net(s) matched" in line and "1 pattern(s) matched nothing" in line


def test_no_manifest_means_no_line_at_all():
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/A"})
    assert netclass.declaration_report(classes, {"capped": 0}) is None


def test_a_declared_net_is_not_proposed_back_to_the_user(tmp_path):
    """Re-proposing a net the user has already ruled on reads as "your file
    was ignored"."""
    intent = _intent(tmp_path, 'nets:\n  - match: "/TP1"\n'
                               '    net_class: other\n')
    classes = netclass.classify(_Schematic([]), set(), set(),
                                board_nets={"/TP1", "/TP2"}, intent=intent)
    document = netclass.propose(classes)
    live = [line for line in document.splitlines()
            if line.startswith('  - match:')]
    assert '  - match: "/TP2"' in live
    assert '  - match: "/TP1"' not in live
