"""Which checks are reported, and which are set aside.

Began as KiCad's *Board Setup > Design Rules > Violation Severity*, which the
user asked for by name. It is **not** that page any more, and the reason is the
more interesting half of the feature.

KiCad offers Error / Warning / Ignore because KiCad has no computed severity to
override — declaring one is the only thing a user *could* do there. This engine
does compute one, from a stated threshold on a measured quantity (HANDOFF §3.2
rule 4). The user, on finding the middle options meaningless:

> In kicad, the whole point of changing severities of items is not to filter for
> whats severe, its to declare how severe things should be. Here, it seems like
> ignoring will remove all checks of this category from consideration (this
> makes sense), but any other severity doesnt mean anything since the engine
> assigns severities not the user.

Exactly right. A user-typed `major` would be a vibe wearing a threshold's
clothes, and it would make two runs of one board disagree for a reason no
measurement could explain. So this page decides one thing per check: **is it
reported, or set aside.** Ranking by severity belongs in the report's own
filter, which is where it now lives.

`ignore` is not silence
-----------------------
A check set to `ignore` still runs and still measures. Its findings are removed
from the run at one seam — `pipeline.apply_policy` — so nothing downstream has
to remember to filter them, which is exactly the bug the first version had. The
**count** still travels, in `summary()` and in the problems panel, because
§2.10 principle 5 does not stop applying when the user asks for quiet: absence
is never shown as a pass.

Reversible, and without a re-review
-----------------------------------
`enabled: false` restores every withheld finding. Nothing is recomputed to do
it: a policy withholds findings a check already produced, so
`Session.reapply_policy` restores, re-applies and rebuilds the report and the
index — 0.58 s against the 63 s audit it replaces.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

#: What a check may be set to. **Two values, and that is the whole point.**
#:
#: This page began as KiCad's Violation Severity, which offers Error/Warning/
#: Ignore — and it offers those because KiCad has no computed severity to
#: override, so declaring one is the only thing a user *could* do there. The
#: user, correctly:
#:
#: > In kicad, the whole point of changing severities of items is not to filter
#: > for whats severe, its to declare how severe things should be. Here, it
#: > seems like ignoring will remove all checks of this category from
#: > consideration (this makes sense), but any other severity doesnt mean
#: > anything since the engine assigns severities not the user.
#:
#: Exactly so, and §3.2 rule 4 says the same thing from the other side:
#: **severity comes from a stated threshold on a computed quantity, never a
#: vibe.** A user-set `major` would be a vibe wearing a threshold's clothes,
#: and it would make two runs of the same board disagree for a reason no
#: measurement could explain. So the only thing this page decides is whether a
#: check's findings are shown at all.
LEVELS = ("report", "ignore")

#: Levels older policy files may carry, from when this page re-levelled. They
#: are read and dropped rather than refused: refusing would make a file written
#: by an earlier version unopenable, and honouring them would reintroduce the
#: thing the user just removed. `load` reports what it dropped.
RETIRED_LEVELS = ("critical", "major", "minor", "info")

#: Written into any policy this module produces, and the only way to tell one
#: from a hand-written file. Deliberately the **shortest** phrase both writers
#: share: the app's Setup tab writes a longer line that still contains this, so
#: a policy written there is recognised here and never needlessly copied aside.
_MARKER = "# severity policy, written by pcbreview"


class SeverityError(Exception):
    """A policy that cannot be used, with the reason."""


def default_path(board_path):
    """Where a board's severity policy lives, if it has one."""
    return Path(board_path).with_suffix(".severity.yaml")


class Policy:
    """One board's severity policy. Immutable once built."""

    def __init__(self, checks: dict | None = None, enabled: bool = True,
                 source=None, dropped=None):
        self.checks = dict(checks or {})
        self.enabled = bool(enabled)
        self.source = source
        #: Retired per-check severities read out of an older policy file and
        #: NOT applied. Reported so the file's owner is told rather than left
        #: to notice that a setting stopped doing anything.
        self.dropped = list(dropped or [])

    def __bool__(self) -> bool:
        return bool(self.checks)

    def level_for(self, check: str) -> str | None:
        return self.checks.get(check)

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "checks": dict(self.checks),
                "path": str(self.source) if self.source else None,
                "dropped": list(self.dropped)}


def load(path) -> Policy:
    """Read a policy. Raises rather than half-loading, like `load_intent`."""
    path = Path(path)
    if not path.exists():
        return Policy()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SeverityError(f"{path}: {exc}") from exc
    if data is None:
        return Policy(source=path)
    if not isinstance(data, dict):
        raise SeverityError(f"{path} is not a severity policy (needs a mapping)")

    checks = data.get("checks") or {}
    if not isinstance(checks, dict):
        raise SeverityError(f"{path}: `checks` must be a mapping of "
                            f"check name to one of {', '.join(LEVELS)}")
    cleaned = {}
    dropped = []
    for name, level in checks.items():
        if level is None or level == "report":
            continue
        if level in RETIRED_LEVELS:
            # Written by a version of this page that re-levelled. Kept
            # readable, not honoured — see `LEVELS`.
            dropped.append(f"{name}={level}")
            continue
        if level not in LEVELS:
            raise SeverityError(
                f"{path}: check '{name}' is set to {level!r}; it must be one "
                f"of {', '.join(LEVELS)}")
        cleaned[str(name)] = level
    return Policy(cleaned, enabled=data.get("enabled", True), source=path,
                  dropped=dropped)


def render(policy: Policy) -> str:
    """The policy as a file a human can read and edit.

    Hand-rendered rather than `yaml.dump`ed, for the reason `facts._render`
    gives: this is a document somebody opens, and the dumper reflows the
    comments that make it readable into something valid and unhelpful.
    """
    lines = [
        _MARKER + ".",
        "#",
        "# Which of pcbreview's checks are reported, and which are set",
        "# aside. Each check may be:",
        "#",
        "#   report   the default. Its findings appear at the severity the",
        "#            check computed from its own stated threshold.",
        "#   ignore   still run it, still measure - but do not show me its",
        "#            findings. This is NOT a pass: the count is stated in the",
        "#            report and in the problems list.",
        "#",
        "# There is deliberately no option to set a severity here. The engine",
        "# computes severity from a threshold on a measured quantity, so a",
        "# severity you typed would be a number with nothing behind it. To rank",
        "# or search by severity, read the report - it is printed in",
        "# severity order within each of its sections.",
        "#",
        "# `enabled: false` restores every withheld finding at once, without",
        "# deleting a line of this file.",
        "",
        f"enabled: {'true' if policy.enabled else 'false'}",
        "",
        "checks:",
    ]
    if not policy.checks:
        lines.append("  {}")
    for name in sorted(policy.checks):
        lines.append(f"  {name}: {policy.checks[name]}")
    return "\n".join(lines) + "\n"


def write(board_path, policy: Policy) -> Path:
    """Write `<board>.severity.yaml`, validated before it lands.

    Same discipline as `facts.write_intent`: render, write beside the target,
    parse it back with this module's own loader, then `os.replace`. A policy
    that would not load is never allowed to become the file.
    """
    target = default_path(board_path)
    document = render(policy)
    temp = target.with_suffix(target.suffix + ".writing")
    temp.write_text(document, encoding="utf-8")
    try:
        load(temp)
    except SeverityError:
        temp.unlink(missing_ok=True)
        raise
    os.replace(temp, target)
    return target


def apply(findings, policy: Policy) -> dict:
    """Re-level findings in place. Returns what moved.

    Mirrors `netclass.apply` deliberately, including that it **mutates** and
    records the previous value. Two differences, both intended:

    * it moves severity in *either* direction, because the user's page does;
    * it applies per check rather than per net, because that is the granularity
      the user chose and the one their reference (KiCad) uses.

    A finding already carrying `severity_before_netclass` keeps it: the two
    records are about different decisions and a reader is entitled to both.
    """
    if not policy.enabled or not policy.checks:
        return {"applied": False, "changed": 0, "ignored": 0, "checks": {}}

    # `changed` is always 0 now and the key is kept because the payload shape
    # is read in three places. Re-levelling was removed, not merely disabled.
    changed = 0
    ignored = 0
    per_check: dict[str, int] = {}
    for finding in findings:
        if policy.checks.get(finding.check) != "ignore":
            # `report`, and "not mentioned at all", are the same thing: the
            # finding keeps the severity its check computed. There is no third
            # option any more — see `LEVELS`.
            continue
        # **`ignore` marks; the caller removes.**
        #
        # An earlier version lowered the finding to `info` and left it in
        # place, so it still appeared in the inspector, on the canvas and in
        # the report. The user set every check to ignore and correctly reported
        # that nothing changed: *"Ignored should just not show the error at all
        # anywhere."*
        #
        # So this only marks, and `pipeline.apply_policy` takes marked findings
        # out of the run before `generate`, `build_index` or `problems.build`
        # ever see them. One seam, so none of the three can forget.
        if not finding.measurement.get("ignored_by_policy"):
            finding.measurement["ignored_by_policy"] = True
            ignored += 1
            per_check[finding.check] = per_check.get(finding.check, 0) + 1

    return {"applied": True, "changed": changed, "ignored": ignored,
            "checks": per_check}


def restore(findings) -> int:
    """Undo a policy, in place. Returns how many findings moved back.

    **This is what makes a policy change need no re-review.** A severity policy
    changes no measurement — it relabels findings a check already computed —
    so applying a different one is `restore` then `apply` over the findings
    already in memory. The user asked exactly this: *"Why does changing
    severity of things require a rerun? Shouldnt it just relabel and refilter
    what gets shown to the user?"* It should, and now it does.

    Contrast `netintent`, which genuinely does need one: declaring a net
    `power` admits it to `check_current_capacity` and `check_heat_path`, so
    there are numbers that do not exist until the audit runs again.

    `apply` records `severity_before_policy` with `setdefault`, so this puts
    back the value the *check* computed however many policies have been
    applied since.
    """
    moved = 0
    for finding in findings:
        measurement = getattr(finding, "measurement", None) or {}
        if "severity_before_policy" in measurement:
            finding.severity = measurement.pop("severity_before_policy")
            moved += 1
        measurement.pop("ignored_by_policy", None)
    return moved


def is_ignored(finding) -> bool:
    measurement = getattr(finding, "measurement", None) or {}
    return bool(measurement.get("ignored_by_policy"))


def summary(policy: Policy, outcome: dict) -> str | None:
    """One line for the report, or `None` when no policy was in force.

    The report is the artefact of record, so a run made under a policy has to
    say so in it. Without this line two reports over the same board could
    differ and nothing in either would explain why.
    """
    if not outcome.get("applied"):
        return None
    parts = []
    if outcome["ignored"]:
        parts.append(f"{outcome['ignored']} withheld by `ignore`")
    settings = ", ".join(f"{name}={level}"
                         for name, level in sorted(policy.checks.items()))
    return (f"a severity policy was applied from "
            f"{Path(policy.source).name if policy.source else 'settings'} "
            f"({settings}) — " + ("; ".join(parts) if parts else
                                  "nothing matched it") +
            ". A check set to `ignore` still RAN and still measured; its "
            "findings are withheld from this report at your instruction and "
            "counted above. Turning the policy off restores every one of them "
            "and every severity its check computed")
