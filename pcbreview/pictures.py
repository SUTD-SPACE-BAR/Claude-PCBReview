"""The pictures every review draws, without being asked.

`python -m pcbreview.run` calls `draw()` after the report and prints what it
drew, so the agent has pictures to show beside the findings they illustrate.

Why by default
--------------
A finding that names `Net-(C29-Pad2)` is useless until someone sees the trace,
and a temperature bracket is a number until someone sees where the heat is.
`SKILL.md` already told the agent to render the findings it reported, and that
instruction was skippable: pictures were drawn when somebody remembered. The
default asked for since is the opposite — every review shows its work — so the
drawing moved into the command, the same way the *selection* moved into
`report/generate.py`. A rule in code is followed every time; a rule in prose is
followed when it is remembered.

What is drawn, and in what order
--------------------------------
1. **the board** — its top copper, whole, so every crop after it has a context;
2. **one picture per group** of five or more (report rule 2), drawn from the
   group's most severe member that has something to draw. A group is one
   cause, so one picture of it is the picture of all of them; a picture per
   member would be dozens of near-identical crops;
3. **every `critical`, then every `major`** the report names that does not
   belong to one of those groups, up to `MAX_SEVERE`;
4. **the power-path maps** — temperature per scenario and current per path —
   whenever a manifest was declared.

The order is the report's own, so the pictures can be shown in the order the
findings are read. Schematic groups are not drawn: a schematic finding is about
a symbol, and the copper it would crop to may not exist.

What a picture highlights
-------------------------
At most `MAX_NETS` nets — as many as `render.HIGHLIGHTS` has colours, so no two
nets in one picture share one. The first measured on the demo board drew a
broadside finding's victim and all 39 of its aggressors, and a crosstalk one
with six nets showed two of them in white: both read as noise.

**Poured planes are left out of the highlight.** A plane covers the board, so
highlighting it highlights everything. A finding whose only nets are planes
and which carries no geometry is not drawn at all — the first version drew a
schematic-parity finding about four `GND` exposed pads as the whole ground
pour.

**A finding that names more nets than there are colours, and carries its own
geometry, is drawn by its geometry alone.** A DRC rule type touches dozens of
nets across the board; the marks at its violations are the picture.

A finding with no net to draw and no geometry has no honest crop: a radius
around a point is a scale somebody would have to invent, which is the refusal
`render.render_finding` already makes. Those are listed as not drawn, with the
command that draws one once somebody chooses the region.

Where the pictures go
---------------------
Not beside the board. The tool writes nothing into a project that the user did
not ask for, so by default the pictures go to the OS cache, one folder per
board, emptied at the start of each run so a picture of an older revision can
never be shown as this one. `--pictures DIR` puts them somewhere the user
chose — the `review/` folder, typically.

A picture failing never fails the review. Each one is attempted on its own,
and whatever went wrong is listed beside the ones that worked.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from pcbreview.render import HIGHLIGHTS
from pcbreview.report.model import LAYOUT, SEVERITY_ORDER

#: How many severe findings outside the groups are drawn. A picture costs
#: about a second once its layer set has been plotted (`render.render`'s
#: `plot_cache`), and a dense board names a hundred majors; past a dozen the
#: pictures stop being looked at. The rest are counted, with the command that
#: draws any one of them.
MAX_SEVERE = 12

#: Nets highlighted in one picture: one per colour `render` has, so no two
#: share a colour and the legend can be read.
MAX_NETS = len(HIGHLIGHTS)

#: The share of a whole-board picture's longer side left above the board for
#: its caption. `render._legend` sizes its band from the same longer side
#: (0.022 per line, plus 0.9 of a line), so a one-line caption needs about
#: 0.042 of it; the rest is air between the band and the copper.
CAPTION_HEADROOM = 0.05


@dataclass
class Planned:
    """One picture `draw()` will attempt."""
    name: str                  # file stem, numbered so the folder sorts in reading order
    kind: str                  # "board" | "group" | "finding"
    caption: str
    finding: object = None     # the engine `Finding` to draw
    note: str = ""             # printed under the caption: what the picture stands for
    nets: list[str] = field(default_factory=list)   # the nets highlighted; [] = geometry only


@dataclass
class Skipped:
    what: str
    reason: str


@dataclass
class Picture:
    path: Path                 # PNG when a browser rasterised it, else the SVG
    kind: str                  # "board" | "group" | "finding" | "power"
    caption: str
    note: str = ""


@dataclass
class Pictures:
    directory: Path | None
    drawn: list[Picture] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    #: why nothing at all could be drawn, when that is the case
    unavailable: str | None = None


# ---------------------------------------------------------------------------
# Selection — pure, so it can be tested without KiCad
# ---------------------------------------------------------------------------

def _key(check: str, title: str, nets) -> tuple:
    return (check, title, tuple(n for n in (nets or ()) if n))


def _subject(finding, plane_nets=frozenset()) -> tuple[list[str] | None, str]:
    """The nets a picture of `finding` highlights, and a note saying so.

    `None` means there is nothing to crop to without inventing a scale, and
    the note is then the reason. `[]` means the finding's own geometry is the
    whole picture.
    """
    nets = [n for n in (finding.nets or ()) if n]
    geometry = getattr(finding, "geometry", None)
    has_geometry = geometry is not None and not geometry.is_empty
    signal = [n for n in nets if n not in plane_nets]
    if has_geometry and len(signal) > MAX_NETS:
        return [], (f"its {len(signal)} nets are not highlighted; the marks "
                    f"are the evidence")
    if not signal:
        if has_geometry:
            return [], ""
        if nets:
            return None, (f"names only poured planes ({', '.join(nets)}) and "
                          f"carries no geometry, so its picture would be the "
                          f"whole plane")
        return None, ("names no net and carries no geometry, so any crop "
                      "would be a guessed scale — draw a region with "
                      "`python -m pcbreview.render <board> <out> --around "
                      "x,y,r`")
    shown = signal[:MAX_NETS]
    note = (f"{len(shown)} of its {len(signal)} nets highlighted, the first "
            f"as the finding lists them" if len(signal) > len(shown) else "")
    return shown, note


def _join(*notes: str) -> str:
    return "; ".join(n for n in notes if n)


def _short(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def plan(report, findings, *, max_severe: int = MAX_SEVERE,
         plane_nets=frozenset()) -> tuple[list[Planned], list[Skipped]]:
    """Which pictures a review draws, in the order they are read.

    `report` is `report.generate`'s `Report`; `findings` is every engine
    `Finding` the layout audit produced (`ReviewResult.all_layout_findings`),
    because only those carry the evidence geometry a picture draws. The two
    are matched on check, title and nets, which is how `generate` built its
    references in the first place. `plane_nets` are the nets poured as zones,
    which are never highlighted.
    """
    index: dict[tuple, object] = {}
    for finding in findings:
        index.setdefault(_key(finding.check, finding.title, finding.nets), finding)

    planned = [Planned(name="01-board", kind="board", caption="")]
    skipped: list[Skipped] = []
    # Every member of a layout group is shown by its group's picture, drawn
    # or not: drawing a group's other members as "severe" pictures would fill
    # the allowance with crops of one cause, which is what grouping removed.
    standing: set[tuple] = {_key(ref.check, ref.title, ref.nets)
                            for group in report.groups if group.source == LAYOUT
                            for ref in group.members}

    for group in report.groups:
        if group.source != LAYOUT:
            continue
        chosen = None
        for ref in group.members:
            finding = index.get(_key(ref.check, ref.title, ref.nets))
            if finding is None:
                continue
            nets, how = _subject(finding, plane_nets)
            if nets is not None:
                chosen = (ref, finding, nets, how)
                break
        cause = group.check + (f" — {group.label} {', '.join(group.shared)}"
                               if group.label else "")
        if chosen is None:
            skipped.append(Skipped(
                f"group {cause} ({group.count} finding(s))",
                "no member has copper other than a plane, or geometry, to "
                "draw"))
            continue
        ref, finding, nets, how = chosen
        planned.append(Planned(
            name=f"{len(planned) + 1:02d}-group-{group.check}", kind="group",
            caption=f"{ref.severity.upper()}  {ref.title}",
            finding=finding, nets=nets,
            note=_join(f"stands for all {group.count} finding(s) that share "
                       f"this cause: {cause}", how)))

    severe = sorted((ref for ref in report.severe if ref.source == LAYOUT),
                    key=lambda r: SEVERITY_ORDER.get(r.severity, 9))
    left_over = 0
    drawn_severe = 0
    for ref in severe:
        key = _key(ref.check, ref.title, ref.nets)
        if key in standing:
            continue
        finding = index.get(key)
        if finding is None:
            continue
        nets, how = _subject(finding, plane_nets)
        if nets is None:
            skipped.append(Skipped(f"{ref.severity.upper()}  {_short(ref.title)}",
                                   how))
            continue
        if drawn_severe >= max_severe:
            left_over += 1
            continue
        standing.add(key)
        drawn_severe += 1
        planned.append(Planned(
            name=f"{len(planned) + 1:02d}-{ref.check}", kind="finding",
            caption=f"{ref.severity.upper()}  {ref.title}", finding=finding,
            nets=nets, note=how))
    if left_over:
        skipped.append(Skipped(
            f"{left_over} more critical/major finding(s)",
            f"past the first {max_severe}; draw any one with `python -m "
            f"pcbreview.render <board> <out> NET`"))
    return planned, skipped


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def default_dir(pcb_path) -> Path:
    """One folder per board, under the OS cache beside the solve cache.

    Keyed on the board's full path as well as its name, so two projects that
    both call their board `main.kicad_pcb` do not empty each other's pictures.
    """
    from pcbreview.solvestore import _os_cache_dir

    pcb_path = Path(pcb_path).resolve()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", pcb_path.stem).strip("-")[:40] or "board"
    digest = hashlib.sha1(str(pcb_path).encode()).hexdigest()[:8]
    return _os_cache_dir() / "pcbreview" / "pictures" / f"{slug}-{digest}"


def _empty(directory: Path) -> None:
    """Remove the pictures a previous run left, and nothing else."""
    if not directory.is_dir():
        return
    for pattern in ("*.png", "*.svg"):
        for old in directory.glob(pattern):
            old.unlink()


def _svg_size(svg: Path) -> tuple[int, int]:
    head = svg.read_text(encoding="utf-8")[:400]
    match = re.search(r'width="([\d.]+)"\s+height="([\d.]+)"', head)
    return ((int(float(match.group(1))), int(float(match.group(2))))
            if match else (1500, 600))


def _svg_title(svg: Path) -> str:
    match = re.search(r"<text[^>]*>([^<]*)</text>",
                      svg.read_text(encoding="utf-8")[:2000])
    return (match.group(1).replace("&amp;", "&").replace("&lt;", "<")
            .replace("&gt;", ">") if match else svg.stem)


def _board_view(board, pcb, directory, name, *, plot_cache, browser, kicad_cli):
    from pcbreview import render as render_module

    x0, y0, x1, y1 = board.outline.bounds
    width, height = x1 - x0, y1 - y0
    headroom = CAPTION_HEADROOM * max(width, height)
    top = board.copper_layers[0]
    caption = f"the board: top copper ({top}), {width:.0f} x {height:.0f} mm"
    rendered = render_module.render(
        board, pcb, [], caption, directory, name, layers=[top],
        region=(x0 - 1, y0 - 1 - headroom, x1 + 1, y1 + 1),
        plot_cache=plot_cache, browser=browser, kicad_cli=kicad_cli)
    return rendered, caption


def draw(result, out_dir=None, *, max_severe: int = MAX_SEVERE,
         browser: str | None = None, kicad_cli: str | None = None) -> Pictures:
    """Draw the review's pictures. Never raises for a picture that fails.

    `result` is `pipeline.review(..., keep_board=True)`'s `ReviewResult`. The
    plots are taken from the user's own board file, not from a refilled copy:
    that copy is deleted when `review` returns.
    """
    from pcbreview import render as render_module
    from pcbreview.drc import find_kicad_cli

    board = result.board
    if board is None:
        return Pictures(None, unavailable=(
            "the board geometry was not kept — call pipeline.review with "
            "keep_board=True"))
    if (kicad_cli or find_kicad_cli()) is None:
        return Pictures(None, unavailable=(
            "kicad-cli was not found, and every picture starts as KiCad's own "
            "plot of the board — set PCBREVIEW_KICAD_CLI"))

    pcb = result.project.board
    directory = Path(out_dir) if out_dir else default_dir(pcb)
    if out_dir is None:
        _empty(directory)
    directory.mkdir(parents=True, exist_ok=True)

    plane_nets = frozenset(z.net_name for z in board.zones
                           if z.net_name and not z.is_rule_area)
    planned, skipped = plan(result.report, result.all_layout_findings,
                            max_severe=max_severe, plane_nets=plane_nets)
    out = Pictures(directory, skipped=skipped)
    plot_cache: dict = {}
    for item in planned:
        try:
            if item.kind == "board":
                rendered, caption = _board_view(
                    board, pcb, directory, item.name, plot_cache=plot_cache,
                    browser=browser, kicad_cli=kicad_cli)
                item.caption = caption
            else:
                # A copy carrying only the nets this picture highlights; the
                # finding in the report is left exactly as it was measured.
                finding = dataclasses.replace(item.finding, nets=item.nets)
                region = None
                if not item.nets:
                    gx0, gy0, gx1, gy1 = finding.geometry.bounds
                    margin = render_module.MARGIN_MM
                    region = (gx0 - margin, gy0 - margin,
                              gx1 + margin, gy1 + margin)
                    if not finding.layers:
                        # Nothing names a layer and nothing is highlighted, so
                        # every copper layer is context.
                        finding = dataclasses.replace(
                            finding, layers=list(board.copper_layers))
                rendered = render_module.render_finding(
                    board, pcb, finding, directory, name=item.name,
                    caption=item.caption, region=region,
                    plot_cache=plot_cache, browser=browser,
                    kicad_cli=kicad_cli)
        except Exception as exc:                          # noqa: BLE001
            out.skipped.append(Skipped(item.caption or item.name,
                                       f"{type(exc).__name__}: {exc}"))
            continue
        out.drawn.append(Picture(rendered.png or rendered.svg, item.kind,
                                 item.caption, item.note))

    power = getattr(result.layout, "power_paths_result", None)
    if power is not None:
        from pcbreview import ampacity

        try:
            maps = ampacity.write_images(power, board, directory)
        except Exception as exc:                          # noqa: BLE001
            maps = []
            out.skipped.append(Skipped("power-path maps",
                                       f"{type(exc).__name__}: {exc}"))
        for svg in maps:
            # Numbered after the rest, so the folder sorts in reading order.
            numbered = svg.with_name(f"{len(out.drawn) + 1:02d}-{svg.name}")
            svg = svg.rename(numbered)
            width, height = _svg_size(svg)
            png = svg.with_suffix(".png")
            shown = (png if render_module.rasterise(svg, png, width, height,
                                                    browser=browser)
                     else svg)
            out.drawn.append(Picture(shown, "power", _svg_title(svg)))
    return out


# ---------------------------------------------------------------------------
# What `run` prints
# ---------------------------------------------------------------------------

_RULE = "=" * 74


def text(pictures: Pictures) -> str:
    """The block `run` prints after the questions."""
    if pictures.unavailable:
        return "\n".join([_RULE, "PICTURES — none drawn", _RULE,
                          f"  {pictures.unavailable}", ""])
    lines = [_RULE,
             f"PICTURES — {len(pictures.drawn)} drawn, in {pictures.directory}",
             _RULE,
             "Show each one to the user beside the finding it illustrates, in",
             "this order, and look at it yourself before you describe it.",
             ""]
    for picture in pictures.drawn:
        lines.append(f"  [{picture.kind}] {picture.path}")
        lines.append(f"      {picture.caption}")
        if picture.note:
            lines.append(f"      {picture.note}")
    if pictures.skipped:
        lines += ["", f"  not drawn — {len(pictures.skipped)}"]
        for item in pictures.skipped:
            lines.append(f"    {item.what}")
            lines.append(f"      {item.reason}")
    lines.append("")
    return "\n".join(lines)
