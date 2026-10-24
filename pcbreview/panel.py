"""Boards that are physically more than one board.

A break-off panel — a section joined by mouse-bites or a routed slot, snapped
off after assembly and often folded back and soldered — looks to every other
check in here like a catastrophe. Nets cross it unrouted, no plane covers the
channel, and the return path for anything running across simply is not there.
On the six-layer board that one feature accounts for a large share of the report,
and a reviewer has to dismiss the same findings every run.

The code cannot know a designer's intent, and does not pretend to. What it can
state exactly is the geometry: **the board's copper falls into more than one
region with no copper joining them.** That is a fact about the file, it is
cheap to compute, and it is the precondition for the intent question rather
than an answer to it.

Detection is a threshold on a measured quantity, and the two populations do not
overlap. Over all eight boards here, the second-largest copper region is:

    the six-layer board       15.98%   <- the break-off panel, 816 mm^2
    the smallest board         2.31%   <- a mounting-hole annulus on a 331 mm^2 board
    a third-party example board    0.88%
    the lightly-routed six-layer board  0.34%
    the four-layer board       0.26%
    the unreferenced four-layer board    0.18%

Everything below 2.4% is a mounting-hole ring or a fiducial. `MIN_FRACTION` is
set at 5%, in the gap, with a factor of two of margin below it and three above.
`MIN_AREA_MM2` is the second half of the test, because a fraction alone scales
wrongly on a very small board — the smallest board's 2.31% is 8 mm^2 of copper
around a 3.2 mm hole.

What this module deliberately does not do is decide that a partitioned board
*is* panelised. A two-part board could equally be a design error. It reports
the partition, marks the findings the channel explains, and leaves the call to
the reviewer — which is `Finding.intent_needed`'s whole job.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import nearest_points, unary_union

from pcbreview.model import Board

#: A copper region must be this much of the board's copper to count as a
#: region rather than an annulus. See the module docstring for the measurement.
MIN_FRACTION = 0.05

#: ...and this much copper outright, so the threshold does not scale down onto
#: mounting-hole rings on a small board.
MIN_AREA_MM2 = 50.0


@dataclass
class CopperRegion:
    """One connected piece of the board's copper, taken across every layer."""
    polygon: Polygon
    area_mm2: float
    fraction: float                  # of the board's total copper area
    nets: set[str] = field(default_factory=set)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.polygon.bounds

    def __str__(self) -> str:
        x0, y0, x1, y1 = self.bounds
        return (f"{self.area_mm2:.0f} mm^2 ({self.fraction:.1%}) over "
                f"x {x0:.1f}-{x1:.1f}, y {y0:.1f}-{y1:.1f}, "
                f"{len(self.nets)} net(s)")


@dataclass
class Partition:
    """How the board's copper divides, and what crosses the divide."""
    regions: list[CopperRegion]           # substantial regions, largest first
    total_copper_mm2: float
    #: narrowest copper-free channel between the main region and any other
    gap_mm: float | None = None
    gap_at: tuple[float, float] | None = None

    @property
    def is_partitioned(self) -> bool:
        return len(self.regions) > 1

    @property
    def main(self) -> CopperRegion | None:
        return self.regions[0] if self.regions else None

    @property
    def detached(self) -> list[CopperRegion]:
        return self.regions[1:]

    def crossing_nets(self) -> set[str]:
        """Nets with copper in the main region *and* in a detached one.

        These are the nets the channel explains. A net wholly inside a detached
        region is routed on a piece of board that happens to be separate and is
        as reviewable as any other; a net that appears on both sides has a
        discontinuity in it by construction, and every return-path, reference
        and continuity finding about it is describing that discontinuity rather
        than a routing mistake.
        """
        if not self.is_partitioned:
            return set()
        elsewhere: set[str] = set()
        for region in self.detached:
            elsewhere |= region.nets
        return self.main.nets & elsewhere

    def region_of(self, point: tuple[float, float]) -> int | None:
        """Index of the region containing a point, or None."""
        probe = Point(*point)
        for index, region in enumerate(self.regions):
            if region.polygon.intersects(probe):
                return index
        return None

    def summary(self) -> dict:
        return {
            "partitioned": self.is_partitioned,
            "regions": len(self.regions),
            "total_copper_mm2": round(self.total_copper_mm2, 1),
            "areas_mm2": [round(r.area_mm2, 1) for r in self.regions],
            "fractions": [round(r.fraction, 4) for r in self.regions],
            "gap_mm": None if self.gap_mm is None else round(self.gap_mm, 3),
            "gap_at": self.gap_at,
            "crossing_nets": sorted(self.crossing_nets()),
        }

    def report(self) -> str:
        if not self.is_partitioned:
            return "copper is one connected region"
        lines = [f"copper falls into {len(self.regions)} regions:"]
        for index, region in enumerate(self.regions):
            lines.append(f"  {'main' if index == 0 else 'detached'}: {region}")
        if self.gap_mm is not None:
            lines.append(f"  narrowest channel {self.gap_mm:.3f} mm at "
                         f"({self.gap_at[0]:.2f}, {self.gap_at[1]:.2f})")
        crossing = sorted(self.crossing_nets())
        if crossing:
            lines.append(f"  {len(crossing)} net(s) have copper on both sides: "
                         + ", ".join(crossing[:8])
                         + (f" and {len(crossing) - 8} more"
                            if len(crossing) > 8 else ""))
        return "\n".join(lines)


def _all_copper(board: Board):
    """Every layer's copper, unioned into one flat geometry.

    Flattening the stack is the point: two regions joined only by a track on
    In2 are not separate, and asking the question per layer would say they
    were. What is being looked for is a place where the fab will cut, and a cut
    goes through all six layers at once.
    """
    parts = [board.copper_on_layer(layer) for layer in board.copper_layers]
    parts = [p for p in parts if p is not None and not p.is_empty]
    return unary_union(parts) if parts else None


def _nets_in(board: Board, polygon: Polygon) -> set[str]:
    """Net names with copper inside a region."""
    names: set[str] = set()
    for track in board.tracks:
        if track.length_mm > 0 and polygon.intersects(track.copper):
            net = board.nets.get(track.net)
            if net is not None and net.name:
                names.add(net.name)
    return names


def partition(board: Board, min_fraction: float = MIN_FRACTION,
              min_area_mm2: float = MIN_AREA_MM2) -> Partition:
    """Split the board's copper into connected regions and measure the divide.

    Returns a `Partition` with a single region for an ordinary board. Regions
    below either threshold — mounting-hole annuli, fiducials, isolated test
    pads — are discarded rather than reported, because a board always has a
    handful and none of them is a partition.
    """
    copper = _all_copper(board)
    if copper is None or copper.is_empty:
        return Partition(regions=[], total_copper_mm2=0.0)

    pieces = (list(copper.geoms) if isinstance(copper, MultiPolygon)
              else [copper])
    total = sum(p.area for p in pieces)
    if total <= 0:
        return Partition(regions=[], total_copper_mm2=0.0)

    pieces.sort(key=lambda p: -p.area)
    kept = [p for p in pieces
            if p.area >= min_area_mm2 and p.area / total >= min_fraction]
    # The largest piece is always a region, however small the board is.
    if not kept:
        kept = pieces[:1]

    regions = [CopperRegion(polygon=p, area_mm2=p.area, fraction=p.area / total,
                            nets=_nets_in(board, p)) for p in kept]

    result = Partition(regions=regions, total_copper_mm2=total)
    if len(regions) > 1:
        best = None
        for other in regions[1:]:
            distance = regions[0].polygon.distance(other.polygon)
            if best is None or distance < best[0]:
                first, second = nearest_points(regions[0].polygon, other.polygon)
                best = (distance, ((first.x + second.x) / 2.0,
                                   (first.y + second.y) / 2.0))
        result.gap_mm, result.gap_at = best
    return result
