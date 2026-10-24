"""Rendering a finding as a picture of the copper it is about.

The brief: tell the user *it is this specific trace*, the way pressing
`` ` `` over a trace in the PCB editor does.

How the overlay is exact rather than registered by eye
-------------------------------------------------------
`kicad-cli pcb export svg --page-size-mode 1` produces an SVG whose **user
units are board millimetres, with an identity transform and the origin at
KiCad's own board origin**. Verified: a plot of a board spanning x 78.97–128.71
carries `viewBox="0 0 297.0022 210.0072"` and `width="297.0022mm"`.

So the shapely geometry this project already validated against KiCad's own
engine — 8e-14 on pad area, 6.6e-6 on track length (§7) — can be written
straight into that SVG as extra `<path>` elements with no coordinate maths at
all. Set the `viewBox` to the finding's bounding box to crop and zoom.

`--page-size-mode 2` also works and offsets by the outline bbox minimum, but
mode 1 needs no offset and is therefore the one to use.

Why we draw the highlight rather than making KiCad draw it
-----------------------------------------------------------
Moving the net's tracks onto a spare user layer in memory and plotting that
layer in another colour would be genuinely "KiCad draws it", and it needs
`pcbnew` — deprecated, gone in KiCad 11 (§4) — a second process, and theme
configuration. Its one advantage would be exact arc and end-cap rendering, and
`Track.copper` is already the same polygon KiCad fills to machine epsilon.

Dimming the board is done by rewriting the colours the plot already wrote
inline (`fill:#C83434`), blending each toward the background. **Do not install a
colour theme into the user's KiCad preferences to achieve this** — this tool
does not write into the user's configuration any more than it writes into their
board.

Portability
-----------
**The SVG is the deliverable and PNG is an upgrade.** SVG needs nothing beyond
`kicad-cli`, renders natively in most chat surfaces and is about a tenth the
size. If no rasteriser is found, the SVG is still returned and `png` is `None`.

Rasterising uses any Chromium-family browser, found by probing, the same house
style as `model.find_kicad_python`. The fact that makes this viable on Windows
is that **Edge is preinstalled on Windows 10 and 11**. `$PCBREVIEW_BROWSER` is
the escape hatch.

`qlmanage` is deliberately not used: macOS-only, *and* it forces a square
canvas and letterboxes a wide crop, which is the wrong shape for almost every
finding.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from pcbreview.drc import find_kicad_cli
from pcbreview.findings import Finding
from pcbreview.model import Board

# ---------------------------------------------------------------------------
# Appearance
# ---------------------------------------------------------------------------

BACKGROUND = (0x0E, 0x16, 0x26)
#: How far to blend the board's own colours toward the background. The
#: highlight has to win against a board that is mostly copper.
DIM = 0.74
MAX_PIXELS = 1500
#: First colour is the finding's subject; the rest distinguish other nets.
HIGHLIGHTS = ("#FFFFFF", "#FF3DC8", "#00E5FF", "#FFD400", "#7CFF6B")
EVIDENCE_COLOUR = "#FF3B30"
#: Stroke width for evidence that is a centreline rather than an area.
#: Wider than a trace on purpose — it has to read against the trace it
#: sits on top of.
EVIDENCE_LINE_MM = 0.30
MARGIN_MM = 3.0

_HEX = re.compile(r"#([0-9A-Fa-f]{6})")
_VIEWBOX = re.compile(r'viewBox="[-\d. ]+"')
_SIZE = re.compile(r'width="[\d.]+mm"\s+height="[\d.]+mm"')


# ---------------------------------------------------------------------------
# Finding a rasteriser
# ---------------------------------------------------------------------------

_BROWSER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "win32": (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ),
    "darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ),
    "linux": (
        "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
        "microsoft-edge", "brave-browser",
    ),
}


class RenderError(Exception):
    """Raised when the SVG itself cannot be produced. A missing rasteriser is
    not one of these — that returns SVG and no PNG."""


def find_browser(explicit: str | None = None) -> str | None:
    """A Chromium-family browser to rasterise with, or `None`.

    `None` is a normal outcome, not a failure: the SVG is the deliverable.
    Probed rather than assumed, because a path existing on this platform says
    nothing about the next one.
    """
    candidates = [explicit, os.environ.get("PCBREVIEW_BROWSER")]
    candidates += list(_BROWSER_CANDIDATES.get(sys.platform, ()))
    for candidate in candidates:
        if not candidate:
            continue
        if Path(candidate).exists():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def rasterise(svg_path: Path, png_path: Path, width: int, height: int,
              browser: str | None = None, timeout: float = 60.0) -> bool:
    """Render an SVG to PNG with a headless browser. `False` if none was found.

    Identical invocation on every platform. `--headless` is the new headless
    mode on current Chrome; a build that rejects it is retried with
    `--headless=old`.
    """
    executable = find_browser(browser)
    if executable is None:
        return False

    for headless in ("--headless", "--headless=old"):
        command = [executable, headless, "--disable-gpu", "--hide-scrollbars",
                   f"--screenshot={png_path}",
                   f"--window-size={width},{height}", str(svg_path)]
        try:
            subprocess.run(command, capture_output=True, timeout=timeout,
                           check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if png_path.exists() and png_path.stat().st_size > 0:
            return True
    return False


# ---------------------------------------------------------------------------
# The board plot
# ---------------------------------------------------------------------------

def plot_layers(pcb_path: str | Path, layers: list[str],
                kicad_cli: str | None = None, timeout: float = 120.0) -> str:
    """KiCad's own plot of the given layers, in board millimetres.

    `--page-size-mode 1` is what makes one SVG user unit one millimetre with
    the origin at KiCad's, so our geometry can be written in unchanged.
    """
    executable = find_kicad_cli(kicad_cli)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "base.svg"
        command = [executable, "pcb", "export", "svg", "--mode-single",
                   "-o", str(out), "--layers", ",".join(layers),
                   "--page-size-mode", "1", "--exclude-drawing-sheet",
                   "--drill-shape-opt", "2"]
        # Feature-detected rather than assumed. `--check-zones` exists in
        # KiCad 10.0.1 and is worth having, but an older `kicad-cli` rejects
        # the whole invocation with `Unknown argument`, which kills rendering
        # outright — and rendering is the part `SKILL.md` insists on. Losing
        # the flag costs nothing here anyway: `Board.from_kicad` has already
        # refilled the zones and reported staleness by the time anything is
        # drawn.
        if _supports(executable, "--check-zones", timeout=timeout):
            command.append("--check-zones")
        command.append(str(pcb_path))
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout)
        if not out.exists():
            message = result.stderr.strip() or result.stdout.strip()
            raise RenderError(
                f"kicad-cli could not plot {Path(pcb_path).name}:\n"
                f"{message}\n"
                f"  kicad-cli used: {executable}\n"
                + ("  'Failed to load board' from a kicad-cli older than the "
                   "board's file format is a version mismatch, not a corrupt "
                   "board. Set PCBREVIEW_KICAD_CLI, or set "
                   "PCBREVIEW_KICAD_PYTHON and the matching kicad-cli beside "
                   "it is used automatically.\n"
                   if "load board" in message.lower() else ""))
        return out.read_text(encoding="utf-8")


#: `(executable, flag) -> bool`, so the help probe runs once per process
#: rather than once per rendered finding.
_FLAG_SUPPORT: dict[tuple[str, str], bool] = {}


def _supports(executable: str, flag: str, timeout: float = 30.0) -> bool:
    """Does this `kicad-cli` accept `flag` on `pcb export svg`?

    A help probe rather than a version comparison: it is version-agnostic and
    it answers the question actually being asked.
    """
    key = (executable, flag)
    if key not in _FLAG_SUPPORT:
        try:
            probe = subprocess.run(
                [executable, "pcb", "export", "svg", "--help"],
                capture_output=True, text=True, timeout=timeout)
            _FLAG_SUPPORT[key] = flag in (probe.stdout + probe.stderr)
        except (OSError, subprocess.TimeoutExpired):
            _FLAG_SUPPORT[key] = False
    return _FLAG_SUPPORT[key]


def dim(svg: str, amount: float = DIM) -> str:
    """Blend every colour the plot wrote toward the background.

    The plot writes its colours inline, so this needs no theme and touches
    nothing in the user's KiCad configuration.
    """
    def blend(match: re.Match) -> str:
        rgb = [int(match.group(1)[i:i + 2], 16) for i in (0, 2, 4)]
        return "#%02X%02X%02X" % tuple(
            round(channel + (target - channel) * amount)
            for channel, target in zip(rgb, BACKGROUND))
    return _HEX.sub(blend, svg)


def path_data(geometry) -> str:
    """Shapely geometry as an SVG path, in board millimetres.

    Handles lines as well as polygons, and that is not incidental: evidence
    geometry is often a line. `reference.Gap.geometry` is the *stretch of trace
    centreline* with no reference beneath it, a MultiLineString of zero area,
    and a polygon-only version of this silently emitted an empty path for every
    return-path finding — the legend showed the colour and the picture showed
    nothing.
    """
    if geometry is None or geometry.is_empty:
        return ""
    parts: list[str] = []

    def walk(shape) -> None:
        if shape is None or shape.is_empty:
            return
        if isinstance(shape, Polygon):
            for ring in [shape.exterior, *shape.interiors]:
                parts.append("M " + " L ".join(f"{x:.4f},{y:.4f}"
                                               for x, y in ring.coords) + " Z")
        elif hasattr(shape, "geoms"):
            for member in shape.geoms:
                walk(member)
        elif hasattr(shape, "coords"):
            coords = list(shape.coords)
            if len(coords) >= 2:
                parts.append("M " + " L ".join(f"{x:.4f},{y:.4f}"
                                               for x, y in coords))

    walk(geometry)
    return " ".join(parts)


def _has_area(geometry) -> bool:
    return geometry is not None and not geometry.is_empty and geometry.area > 0


def net_geometry(board: Board, net: str, layers: list[str]):
    """Every piece of copper a net has on the given layers, unioned."""
    try:
        code = board.net(net).code
    except KeyError:
        return None
    pieces = [t.copper for t in board.tracks_on_net(code) if t.layer in layers]
    for via in board.vias_on_net(code):
        for layer in layers:
            if layer in via.layers:
                pieces.append(via.pad_on(layer))
    pieces += [p.copper for p in board.pads_on_net(code)
               if any(p.on_layer(layer) for layer in layers)]
    pieces = [g for g in pieces if g is not None and not g.is_empty]
    return unary_union(pieces) if pieces else None


def layers_used_by(board: Board, nets: list[str]) -> list[str]:
    used: set[str] = set()
    for name in nets:
        try:
            code = board.net(name).code
        except KeyError:
            continue
        used |= {t.layer for t in board.tracks_on_net(code)}
    return [layer for layer in board.copper_layers if layer in used]


# ---------------------------------------------------------------------------
# Composing
# ---------------------------------------------------------------------------

@dataclass
class Rendered:
    """What one render produced. `png` is `None` when no browser was found."""
    svg: Path
    png: Path | None
    width_mm: float
    height_mm: float
    layers: list[str]
    nets: list[str]
    note: str | None = None

    def __str__(self) -> str:
        size = f"{self.width_mm:.1f} x {self.height_mm:.1f} mm"
        return (f"{self.png or self.svg} ({size}, {', '.join(self.layers)})"
                + (f" — {self.note}" if self.note else ""))


#: Roughly how wide one character is as a fraction of the font size, for
#: Helvetica at these weights. Only used to decide where to break a line, so
#: it needs to be about right, not exact.
_CHAR_WIDTH = 0.52


def _wrap(caption: str, width: float, font_size: float) -> list[str]:
    """Break a caption to fit the crop's width, on word boundaries.

    Captions used to be emitted as a single `<text>` element, so anything
    longer than the image was **silently clipped mid-sentence** — `"...and all
    three cross the"`. `SKILL.md` says the caption is where the explanation
    goes, so truncating it defeats the stated purpose of the feature.
    """
    limit = max(8, int(width / (font_size * _CHAR_WIDTH)))
    lines: list[str] = []
    current = ""
    for word in str(caption).split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _legend(x: float, y: float, width: float, height: float, caption: str,
            entries: list[tuple[str, str]], layers: list[str]) -> list[str]:
    """A caption block drawn in board units at the top-left of the crop.

    The band is sized to the wrapped caption rather than assuming one line,
    so a long explanation grows the block instead of being cut off.
    """
    scale = max(width, height)
    pad = scale * 0.012
    line = scale * 0.022
    caption_size = line * 0.50
    wrapped = _wrap(caption, width - 2 * pad, caption_size)
    head = len(wrapped)
    out = [f'<rect x="{x}" y="{y}" width="{width}" '
           f'height="{line * (len(entries) + 0.9 + head):.4f}" fill="#000000" '
           f'fill-opacity="0.55"/>']
    for index, text in enumerate(wrapped):
        out.append(
            f'<text x="{x + pad:.4f}" y="{y + line * (1.1 + index * 0.72):.4f}" '
            f'fill="#FFFFFF" font-family="Helvetica,Arial,sans-serif" '
            f'font-size="{caption_size:.4f}" font-weight="600">'
            f'{_escape(text)}</text>')
    for index, (label, colour) in enumerate(entries):
        row = y + line * (1.1 + head * 0.72 + 0.9 + index)
        out.append(
            f'<rect x="{x + pad:.4f}" y="{row - line * 0.42:.4f}" '
            f'width="{line * 0.5:.4f}" height="{line * 0.5:.4f}" '
            f'fill="{colour}"/>'
            f'<text x="{x + pad + line * 0.75:.4f}" y="{row:.4f}" '
            f'fill="#D8DEE9" font-family="Helvetica,Arial,sans-serif" '
            f'font-size="{line * 0.44:.4f}">{_escape(label)}   '
            f'[{_escape(", ".join(layers))}]</text>')
    return out


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def render(board: Board, pcb_path: str | Path, nets: list[str], caption: str,
           out_dir: str | Path, name: str,
           layers: list[str] | None = None,
           marker: tuple[float, float] | None = None,
           evidence=None,
           evidence_label: str = "evidence",
           margin_mm: float = MARGIN_MM,
           browser: str | None = None,
           kicad_cli: str | None = None,
           region: tuple[float, float, float, float] | None = None,
           plot_cache: dict | None = None) -> Rendered:
    """Plot the board, dim it, and draw the finding's own copper on top.

    `evidence` is optional shapely geometry drawn in a distinct colour — the
    gap polygons for a return-path finding, the coupled run for a crosstalk
    one. Drawing only the net is the easy half and the less useful one.

    `region` is `(x0, y0, x1, y1)` in board millimetres and **crops to that box
    instead of to the copper**, which is what lets a finding that names no net
    be drawn at all. `thermal`, `placement` and `stackup_mismatch` name no net,
    and those are precisely the findings that most need a picture: the single
    most important measurement of one outside review was a plane constriction —
    0.755 mm2 of copper summed over three layers at one x, carrying ~26 A — and
    there was no way to illustrate it. With a region, `nets` may be empty.

    `plot_cache` is a dict the caller keeps across several renders of one
    board. The `kicad-cli` plot is the slow half of a picture and depends only
    on the board and the layer set, so a review drawing a dozen pictures plots
    each layer set once instead of once per picture (`pictures.draw`).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    layers = (layers or layers_used_by(board, nets)
              or [board.copper_layers[0]])
    plot_key = (str(pcb_path), tuple(layers))
    plotted = plot_cache.get(plot_key) if plot_cache is not None else None
    if plotted is None:
        plotted = plot_layers(pcb_path, list(layers) + ["Edge.Cuts"],
                              kicad_cli=kicad_cli)
        if plot_cache is not None:
            plot_cache[plot_key] = plotted

    geometries = {net: net_geometry(board, net, layers) for net in nets}
    drawn = [g for g in geometries.values() if g is not None and not g.is_empty]
    if evidence is not None and not evidence.is_empty:
        drawn.append(evidence)
    if not drawn and region is None:
        raise RenderError(f"nothing to draw: none of {nets} has copper on "
                          f"{', '.join(layers)}")

    # Dimming exists to make the highlight stand out against the rest of the
    # board. A region render with no net named has no highlight, so dimming it
    # only produces a muddy picture of the copper the reader came to look at.
    svg = dim(plotted) if drawn else plotted

    if region is not None:
        # An explicit crop is taken verbatim — no margin. The caller chose the
        # box, and silently growing it would make `--bbox` not mean what it
        # says. `--around x,y,r` builds its box from the radius and gets the
        # same treatment.
        min_x, min_y, max_x, max_y = region
        if max_x <= min_x or max_y <= min_y:
            raise RenderError(
                f"region {region} is empty: x1 must exceed x0 and y1 must "
                f"exceed y0, in board millimetres")
    else:
        min_x, min_y, max_x, max_y = unary_union(drawn).bounds
        min_x -= margin_mm
        min_y -= margin_mm
        max_x += margin_mm
        max_y += margin_mm
    width = max_x - min_x
    height = max_y - min_y
    scale = min(MAX_PIXELS / width, MAX_PIXELS / height)

    overlay: list[str] = []
    entries: list[tuple[str, str]] = []
    for index, (net, geometry) in enumerate(geometries.items()):
        if geometry is None or geometry.is_empty:
            continue
        colour = HIGHLIGHTS[index % len(HIGHLIGHTS)]
        data = path_data(geometry)
        overlay.append(
            f'<g><path d="{data}" fill="none" stroke="{colour}" '
            f'stroke-width="0.45" stroke-opacity="0.30" '
            f'stroke-linejoin="round"/>'
            f'<path d="{data}" fill="{colour}" fill-opacity="0.92" '
            f'fill-rule="evenodd" stroke="{colour}" stroke-width="0.03"/></g>')
        entries.append((net, colour))

    if evidence is not None and not evidence.is_empty:
        # A filled polygon wants a hairline outline; a bare centreline needs a
        # stroke wide enough to see and no fill at all, or an open path gets
        # filled across its own implicit closure.
        if _has_area(evidence):
            style = (f'fill="{EVIDENCE_COLOUR}" fill-opacity="0.35" '
                     f'stroke="{EVIDENCE_COLOUR}" stroke-width="0.06" '
                     f'fill-rule="evenodd"')
        else:
            style = (f'fill="none" stroke="{EVIDENCE_COLOUR}" '
                     f'stroke-width="{EVIDENCE_LINE_MM}" stroke-opacity="0.95" '
                     f'stroke-linecap="round"')
        overlay.append(f'<path d="{path_data(evidence)}" {style}/>')
        entries.append((evidence_label, EVIDENCE_COLOUR))

    if marker:
        radius = max(width, height) * 0.03
        overlay.append(
            f'<circle cx="{marker[0]:.4f}" cy="{marker[1]:.4f}" '
            f'r="{radius:.4f}" fill="none" stroke="{EVIDENCE_COLOUR}" '
            f'stroke-width="{radius * 0.16:.4f}"/>'
            f'<circle cx="{marker[0]:.4f}" cy="{marker[1]:.4f}" '
            f'r="{radius * 0.12:.4f}" fill="{EVIDENCE_COLOUR}"/>')

    overlay += _legend(min_x, min_y, width, height, caption, entries,
                       list(layers))

    pixel_width = max(int(width * scale), 1)
    pixel_height = max(int(height * scale), 1)
    svg = _VIEWBOX.sub(
        f'viewBox="{min_x:.4f} {min_y:.4f} {width:.4f} {height:.4f}"', svg, 1)
    svg = _SIZE.sub(f'width="{pixel_width}" height="{pixel_height}"', svg, 1)
    # A dark backing rectangle behind everything, so the crop is not white.
    backing = (f'<rect x="{min_x:.4f}" y="{min_y:.4f}" width="{width:.4f}" '
               f'height="{height:.4f}" fill="#%02X%02X%02X"/>' % BACKGROUND)
    if "<title>" in svg:
        svg = svg.replace("<title>", backing + "<title>", 1)
    else:
        svg = svg.replace("</svg>", backing + "</svg>", 1)
    svg = svg.replace("</svg>", "\n".join(overlay) + "\n</svg>")

    svg_path = out_dir / f"{name}.svg"
    svg_path.write_text(svg, encoding="utf-8")

    png_path = out_dir / f"{name}.png"
    note = None
    if not rasterise(svg_path, png_path, pixel_width, pixel_height,
                     browser=browser):
        png_path = None
        note = ("no Chromium-family browser found, so no PNG — the SVG is the "
                "deliverable and renders natively in most viewers. Set "
                "PCBREVIEW_BROWSER to rasterise.")

    return Rendered(svg=svg_path, png=png_path, width_mm=width,
                    height_mm=height, layers=list(layers), nets=list(nets),
                    note=note)


def stem_for(nets: list[str], prefix: str = "",
             layers: list[str] | None = None,
             caption: str | None = None,
             region: tuple[float, float, float, float] | None = None) -> str:
    """A filename stem that cannot collide with another *render*.

    `nets[0]` alone is not enough, and not only for the obvious reason. The
    non-alphanumeric substitution maps `/D+` and `/D-` to the same slug, so two
    renders of different single nets overwrote each other silently, as did a
    six-net render followed by a two-net one starting with the same net
    (§48.3 item 7).

    **The nets alone are not enough either, and that was this function's own
    bug.** The first version digested only the net names, so the most natural
    two-render workflow on a two-layer board —

        render … GND --layers F.Cu
        render … GND --layers B.Cu

    — produced one file twice and silently lost the first image. `SKILL.md`
    had meanwhile been given the sentence *"two renders never overwrite each
    other"*, which made it a documented guarantee that was false. Everything
    that distinguishes one picture from another now goes into the digest.
    """
    # Leaf names only. A hierarchical net is
    # `/A Long Sheet Name/SIGNAL_A`, and keeping the sheet path repeated it
    # once per net and ate the whole budget before the net names arrived:
    # `A-Long-Sheet-Name-SIGNAL-A-A-Long-Sheet-Name-SIGNAL--4bd931.png`.
    # The digest already guarantees uniqueness, so the slug only has to be
    # recognisable.
    leaves = [name.rstrip("/").rsplit("/", 1)[-1] or name for name in nets]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", "-".join(leaves)).strip("-")[:60]
    if not slug and region is not None:
        # A region render names no net, so the box is the only recognisable
        # thing about it. Without this every netless render would be called
        # bare `region-<digest>` and be impossible to tell apart at a glance.
        slug = "region-{:.0f}-{:.0f}".format(region[0], region[1])
    material = ("\n".join(nets) + "\x1e" + ",".join(layers or ()) + "\x1e"
                + (caption or "")
                # The region is part of what distinguishes one picture from
                # another, for the same reason the layers are (§51.5 item 9).
                + "\x1e" + (",".join(f"{v:.4f}" for v in region)
                            if region else ""))
    digest = hashlib.sha1(material.encode()).hexdigest()[:6]
    return "-".join(part for part in (prefix, slug, digest) if part)


def render_finding(board: Board, pcb_path: str | Path, finding: Finding,
                   out_dir: str | Path, name: str | None = None,
                   radius_mm: float | None = None,
                   caption: str | None = None,
                   **kwargs) -> Rendered:
    """Render one audit finding, using whatever evidence geometry it carries.

    A `Finding` already knows its `nets`, `layers` and `location`. Checks that
    populate `Finding.geometry` get their own evidence drawn on top — that is
    the point at which this stops being a screenshot and starts being evidence.

    **A finding naming no net can now be drawn**, provided it has a `location`
    and a `radius_mm` is given: the crop is taken around that point instead of
    around copper. `thermal`, `placement` and `stackup_mismatch` are the netless
    checks and the ones an outside review most wanted pictures of. Without a
    radius the old refusal stands, because guessing a scale for someone else's
    board is exactly the kind of invented number this project refuses.

    `caption` replaces the default `SEVERITY  title` band — a review's
    pictures say which group a finding stands for, which the finding itself
    does not know.
    """
    nets = [n for n in finding.nets if n]
    region = kwargs.pop("region", None)
    if not nets and region is None:
        if finding.location is None or radius_mm is None:
            raise RenderError(
                f"{finding.check} finding names no nets to draw"
                + ("" if finding.location is None else
                   " — pass radius_mm to crop around its location instead"))
        x, y = finding.location
        region = (x - radius_mm, y - radius_mm, x + radius_mm, y + radius_mm)
    caption = caption or f"{finding.severity.upper()}  {finding.title}"
    return render(
        board, pcb_path, nets, caption, out_dir,
        name or stem_for(nets, prefix=finding.check,
                         layers=finding.layers or None,
                         caption=caption, region=region),
        layers=finding.layers or None,
        marker=finding.location,
        evidence=finding.geometry,
        evidence_label=finding.geometry_label or "evidence",
        region=region,
        **kwargs)


_USAGE = """\
usage: python -m pcbreview.render <board.kicad_pcb> <out_dir> [net ...]
                                 [--layers F.Cu,In1.Cu] [--caption TEXT]
                                 [--bbox x0,y0,x1,y1 | --around x,y,r]

  --caption TEXT       printed under the image; this is where the explanation
                       goes
  --layers L,L         restrict the drawing to these copper layers
  --bbox x0,y0,x1,y1   crop to this box in board millimetres instead of to the
                       named nets' copper. Nets become optional
  --around x,y,r       the same thing from a centre and a radius

A finding that names no net — `thermal`, `placement`, `stackup_mismatch` — has
no copper to crop to, so it needs one of the region options and its `location`
is the natural centre. With --bbox or --around you may name nets as well, and
they are still highlighted; the crop just stops depending on them.

The output stem is built from every net named, the layers, the caption and the
region plus a short digest, so two renders never overwrite each other.
"""


def _parse_region(bbox: str | None, around: str | None
                  ) -> tuple[float, float, float, float] | None:
    """`--bbox x0,y0,x1,y1` or `--around x,y,r` -> a box, or `None`."""
    if bbox and around:
        raise RenderError("give --bbox or --around, not both")
    if bbox:
        values = [float(v) for v in bbox.split(",")]
        if len(values) != 4:
            raise RenderError(
                f"--bbox needs four comma-separated numbers "
                f"(x0,y0,x1,y1), got {len(values)}: {bbox!r}")
        return (values[0], values[1], values[2], values[3])
    if around:
        values = [float(v) for v in around.split(",")]
        if len(values) != 3:
            raise RenderError(
                f"--around needs three comma-separated numbers (x,y,r), "
                f"got {len(values)}: {around!r}")
        x, y, radius = values
        if radius <= 0:
            raise RenderError(f"--around radius must be positive, got {radius}")
        return (x - radius, y - radius, x + radius, y + radius)
    return None


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write(_USAGE)
        return 2

    pcb, out_dir = argv[1], argv[2]
    rest = argv[3:]
    layers = None
    caption = None
    bbox = around = None
    nets = []
    index = 0
    while index < len(rest):
        if rest[index] in ("--layers", "--caption", "--bbox", "--around"):
            if index + 1 >= len(rest):
                sys.stderr.write(f"{rest[index]} needs a value\n")
                return 2
            value = rest[index + 1]
            if rest[index] == "--layers":
                layers = value.split(",")
            elif rest[index] == "--caption":
                caption = value
            elif rest[index] == "--bbox":
                bbox = value
            else:
                around = value
            index += 2
        else:
            nets.append(rest[index])
            index += 1

    try:
        region = _parse_region(bbox, around)
    except RenderError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    if not nets and region is None:
        sys.stderr.write("name at least one net, or give --bbox / --around\n")
        return 2

    board = Board.from_kicad(pcb)
    default_caption = ", ".join(nets) or (
        "region {:.2f},{:.2f} to {:.2f},{:.2f} mm".format(*region))
    try:
        result = render(board, pcb, nets, caption or default_caption, out_dir,
                        stem_for(nets, layers=layers, caption=caption,
                                 region=region),
                        layers=layers, region=region)
    except RenderError as exc:
        # A refusal is a message to the user, not a traceback. An inverted
        # --bbox is the easy one to type and used to print a stack.
        sys.stderr.write(f"{exc}\n")
        return 2
    print(result)
    if result.note:
        print(f"note: {result.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
