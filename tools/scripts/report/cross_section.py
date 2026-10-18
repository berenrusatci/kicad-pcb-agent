#!/usr/bin/env python3
"""Render a millimetre region of the board to a PNG an agent can actually see.

Reading an SVG with a text tool gives you a coordinate list, which is the blind
-coordinate problem you already had. This chain produces real pixels:

    kicad-cli pcb export svg  ->  svglib+reportlab (SVG->PDF)  ->  PDF->PNG
    ->  crop to the requested mm rectangle  ->  scale

backend: cli  (kicad-cli + one of pdftocairo / pdftoppm; plus the optional
Python tier below)

OPTIONAL DEPENDENCY TIER - THE ONLY ONE IN THE BUNDLE
-----------------------------------------------------
This is the single tool that needs packages beyond the standard library:
``svglib``, ``reportlab`` and ``Pillow``. They are imported INSIDE
``render_region()`` and nowhere else, so:

  * importing this module is stdlib-only,
  * ``--self-test`` runs without them,
  * the whole test suite collects and runs without them,
  * and when they are genuinely missing the tool fails with one clear,
    actionable message naming the packages - not an ImportError traceback from
    the middle of a render.

Install with: ``pip install svglib reportlab pillow`` plus poppler-utils for
``pdftocairo``.

ACCURACY BOUNDARY - READ BEFORE USING A NUMBER FROM THIS
--------------------------------------------------------
The mapping assumes the exported SVG's (0,0) corner sits on the Edge.Cuts bbox
(x_min, y_min) and that one SVG unit is exactly 1 mm. Measured against a real
board, that assumption was off by about 0.06 mm, because a circular outline is
arc-sampled and the bbox derived from control points is slightly tight.

THIS OUTPUT IS FOR VISUAL ORIENTATION ONLY. It is not admissible as a
measurement and must not drive a DRC or fabrication decision - those belong to
the kicad-cli and pcbnew based checkers. Placement correctness in particular
should be checked with ``scripts/board/collision_radar.py``, which produces
numbers, not pictures.

Usage:
    cross_section.py <board.kicad_pcb> --region x1,y1,x2,y2
                     [--layers F.Cu,Edge.Cuts] [--out section.png]
                     [--dpi 1200] [--zoom 2] [--json out.json]
    cross_section.py --self-test

Exit: 0 = rendered, 1 = could not render, 2 = run error.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import FAIL, board_report, result  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "cross-section-report/v1"
TOOL_NAME = "cross_section"

OPTIONAL_PACKAGES = ("svglib", "reportlab", "pillow")
PDF_TO_PNG_CANDIDATES = ("pdftocairo", "pdftoppm")


class OptionalDependencyMissing(RuntimeError):
    """The optional render tier is not installed. Actionable by design."""


@dataclass(frozen=True)
class Bounds:
    x_min: float
    y_min: float
    x_max: float
    y_max: float

    def __post_init__(self) -> None:
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError(f"degenerate bounds: {self}")

    @property
    def width(self) -> float:
        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        return self.y_max - self.y_min


# --------------------------------------------------------------------------- #
# Edge.Cuts bbox (stdlib only)
# --------------------------------------------------------------------------- #
def _block_end(text: str, start: int) -> int:
    """Index of the ``)`` matching the ``(`` at ``start``, skipping strings.

    A naive regex looking for a closing paren stops at the inner
    ``(stroke ...)`` of a ``gr_circle`` and never reaches the real closing
    paren, producing a wrong bbox. That was a real regression on a circular
    board outline, so the depth counter stays.
    """
    depth = 0
    i = start
    n = len(text)
    while i < n:
        char = text[i]
        if char == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unterminated S-expression block (corrupt .kicad_pcb?)")


_SHAPE = re.compile(r"\((gr_line|gr_arc|gr_circle|gr_rect|gr_poly)\b")
_POINT = re.compile(r"\((?:start|end|mid|center|xy)\s+(-?[\d.]+)\s+(-?[\d.]+)\)")
_CENTER = re.compile(r"\(center\s+(-?[\d.]+)\s+(-?[\d.]+)\)")
_END = re.compile(r"\(end\s+(-?[\d.]+)\s+(-?[\d.]+)\)")


def bounds_from_text(text: str) -> Bounds:
    """Edge.Cuts bounding box.

    LIMIT: arcs contribute only their start/mid/end control points, not the
    true bulge, so a curved outline can come out slightly tight (~0.06 mm was
    measured). Fine for cropping, not for measurement. Circles ARE expanded to
    centre +/- radius, because a circle's ``end`` point is one point on the
    circumference and taking it literally collapses the bbox onto one edge.
    """
    points: list[tuple[float, float]] = []
    for match in _SHAPE.finditer(text):
        start = match.start()
        block = text[start:_block_end(text, start) + 1]
        if "Edge.Cuts" not in block:
            continue
        center, end = _CENTER.search(block), _END.search(block)
        if match.group(1) == "gr_circle" and center and end:
            cx, cy = float(center.group(1)), float(center.group(2))
            ex, ey = float(end.group(1)), float(end.group(2))
            radius = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
            points += [(cx - radius, cy - radius), (cx + radius, cy + radius)]
        else:
            points += [(float(m.group(1)), float(m.group(2)))
                       for m in _POINT.finditer(block)]
    if not points:
        raise ValueError(
            "no Edge.Cuts geometry found - the board has no outline, it is on "
            "another layer, or the .kicad_pcb format is not what was expected.")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return Bounds(min(xs), min(ys), max(xs), max(ys))


def board_bounds(board_path: str) -> Bounds:
    return bounds_from_text(Path(board_path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Optional tier
# --------------------------------------------------------------------------- #
def optional_tier_status() -> dict:
    """What is installed, without importing anything heavy."""
    import importlib.util
    modules = {"svglib": "svglib.svglib", "reportlab": "reportlab.graphics",
               "pillow": "PIL.Image"}
    present = {}
    for name, module in modules.items():
        try:
            present[name] = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            present[name] = False
    converter = next((c for c in PDF_TO_PNG_CANDIDATES if shutil.which(c)), None)
    return {"packages": present, "pdf_to_png": converter,
            "complete": all(present.values()) and converter is not None}


def _require_optional_tier():
    """Import the optional tier or raise ONE clear, actionable error."""
    status = optional_tier_status()
    missing = [name for name, ok in status["packages"].items() if not ok]
    if status["pdf_to_png"] is None:
        missing.append("poppler-utils (pdftocairo or pdftoppm)")
    if missing:
        raise OptionalDependencyMissing(
            "cross_section needs the optional render tier, which is not "
            f"installed. Missing: {', '.join(missing)}. Install with: "
            "pip install svglib reportlab pillow   (and your platform's "
            "poppler-utils package). Every other tool in this bundle is "
            "stdlib-only and is unaffected.")
    from PIL import Image
    from reportlab.graphics import renderPDF
    from svglib.svglib import svg2rlg
    return svg2rlg, renderPDF, Image, status["pdf_to_png"]


def render_region(board_path: str, out_png: str, x1: float, y1: float,
                  x2: float, y2: float, layers=("F.Cu", "Edge.Cuts"),
                  dpi: int = 1200, zoom: float = 2.0,
                  kicad_cli: str | None = None,
                  workdir: str | None = None) -> str:
    """Render the (x1,y1)-(x2,y2) mm rectangle to a PNG. Returns the path.

    Every optional import happens here and nowhere else - see the module
    docstring.
    """
    svg2rlg, renderPDF, Image, pdf_to_png = _require_optional_tier()

    from common.kicad_cli import export_pcb_svg

    bounds = board_bounds(board_path)
    work = Path(workdir) if workdir else Path(out_png).resolve().parent
    work.mkdir(parents=True, exist_ok=True)
    svg_path = work / "_cross_section.svg"
    pdf_path = work / "_cross_section.pdf"
    png_path = work / "_cross_section.png"

    export_pcb_svg(board_path, str(svg_path), ",".join(layers),
                   fit_page_to_board=True, single_page=True, cli=kicad_cli)

    renderPDF.drawToFile(svg2rlg(str(svg_path)), str(pdf_path))

    proc = subprocess.run(
        [pdf_to_png, "-png", "-r", str(dpi), "-singlefile",
         str(pdf_path), str(png_path.with_suffix(""))],
        capture_output=True, text=True)
    if proc.returncode != 0 or not png_path.exists():
        raise RuntimeError(
            f"{pdf_to_png} failed to rasterise:\n{proc.stdout}\n{proc.stderr}")

    full = Image.open(png_path)
    px_per_mm = full.width / bounds.width

    def to_px(x_mm: float, y_mm: float):
        return ((x_mm - bounds.x_min) * px_per_mm,
                (y_mm - bounds.y_min) * px_per_mm)

    left, top = to_px(min(x1, x2), min(y1, y2))
    right, bottom = to_px(max(x1, x2), max(y1, y2))
    left, top = max(0, int(left)), max(0, int(top))
    right = min(full.width, int(right))
    bottom = min(full.height, int(bottom))
    if right <= left or bottom <= top:
        raise ValueError(
            f"the requested region ({x1},{y1})-({x2},{y2}) is outside the board "
            f"bounds ({bounds.x_min},{bounds.y_min})-"
            f"({bounds.x_max},{bounds.y_max})")

    section = full.crop((left, top, right, bottom))
    if zoom != 1.0:
        section = section.resize(
            (max(1, int(section.width * zoom)),
             max(1, int(section.height * zoom))), Image.LANCZOS)
    section.save(out_png)

    for temporary in (svg_path, pdf_path, png_path):
        temporary.unlink(missing_ok=True)
    return out_png


# --------------------------------------------------------------------------- #
# Both fixtures below carry a full (version)/(generator)/(general)/(paper)/
# (layers) header, not just the graphic items. bounds_from_text() (the
# self-test's own parser) does not need it, but kicad-cli DOES: fed a header-
# less board, `kicad-cli pcb export svg` segfaults instead of erroring
# (verified against kicad-cli 10.0.4) - CliContract's tests write these to a
# real .kicad_pcb and exercise the real CLI end to end, so the header is load-
# bearing, not decoration.
_BOARD_HEADER = """(version 20221018) (generator pcbnew)
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user)
    (44 "Edge.Cuts" user)
  )"""

_RECT_BOARD = f"""(kicad_pcb {_BOARD_HEADER}
  (gr_line (start -10 -5) (end 10 -5) (layer "Edge.Cuts") (uuid "a"))
  (gr_line (start 10 -5) (end 10 5) (layer "Edge.Cuts") (uuid "b"))
  (gr_line (start 10 5) (end -10 5) (layer "Edge.Cuts") (uuid "c"))
  (gr_line (start -10 5) (end -10 -5) (layer "Edge.Cuts") (uuid "d"))
  (gr_line (start 0 0) (end 1 1) (layer "F.Cu") (uuid "e"))
)
"""

_ROUND_BOARD = f"""(kicad_pcb {_BOARD_HEADER}
  (gr_circle
    (center 0 0)
    (end 21 0)
    (stroke (width 0.1) (type default))
    (fill no)
    (layer "Edge.Cuts")
    (uuid "x")
  )
)
"""


def self_test() -> list[str]:
    """Parser only - runs with the optional tier ABSENT."""
    problems: list[str] = []

    bounds = bounds_from_text(_RECT_BOARD)
    if (bounds.x_min, bounds.y_min, bounds.x_max, bounds.y_max) != (-10, -5, 10, 5):
        problems.append(f"rectangular bbox wrong: {bounds}")
    if bounds.width != 20 or bounds.height != 10:
        problems.append("width/height properties are wrong")

    # FAULT INJECTION: the F.Cu line must NOT widen the Edge.Cuts bbox.
    if bounds.x_max != 10:
        problems.append("an F.Cu line leaked into the Edge.Cuts bbox")

    try:
        Bounds(0, 0, 0, 5)
    except ValueError:
        pass
    else:
        problems.append("a zero-width Bounds was accepted")

    try:
        bounds_from_text('(kicad_pcb (gr_line (start 0 0) (end 1 1) '
                         '(layer "F.Cu")))')
    except ValueError:
        pass
    else:
        problems.append("a board with no Edge.Cuts did not raise")

    # NESTED BLOCK REGRESSION: gr_circle contains (stroke (width ..) (type ..)).
    circle = bounds_from_text(_ROUND_BOARD)
    if (circle.x_min, circle.y_min, circle.x_max, circle.y_max) != (-21, -21, 21, 21):
        problems.append(f"circular outline bbox wrong: {circle}")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", nargs="?", help="path to the .kicad_pcb")
    parser.add_argument("--region", help="x1,y1,x2,y2 in mm, e.g. 1,-5,15,4")
    parser.add_argument("--layers", default="F.Cu,Edge.Cuts")
    parser.add_argument("--out", default="section.png")
    parser.add_argument("--dpi", type=int, default=1200)
    parser.add_argument("--zoom", type=float, default=2.0)
    parser.add_argument("--kicad-cli", help="explicit kicad-cli path")
    parser.add_argument("--json", help="also write the JSON report here")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (opt-in)")
    args = parser.parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        if problems:
            return 1
        diag("SELF-TEST PASS: cross_section")
        return 0

    if not args.board:
        parser.error("the board path is required")
    if not args.region:
        parser.error("--region x1,y1,x2,y2 (mm) is required")

    try:
        x1, y1, x2, y2 = (float(v) for v in args.region.split(","))
    except ValueError:
        parser.error("--region must be four comma-separated numbers: x1,y1,x2,y2")

    tier = optional_tier_status()
    bounds = board_bounds(args.board)

    try:
        path = render_region(args.board, args.out, x1, y1, x2, y2,
                             layers=args.layers.split(","), dpi=args.dpi,
                             zoom=args.zoom, kicad_cli=args.kicad_cli)
        finding = result("cross_section_rendered", 1, [], f"wrote {path}")
        rendered = path
    except OptionalDependencyMissing as exc:
        diag(f"NO COVERAGE: {exc}")
        finding = result("cross_section_rendered", 0, [], str(exc))
        rendered = None

    report = board_report(TOOL_NAME, args.board, [finding], params={
        "region_mm": [x1, y1, x2, y2],
        "layers": args.layers.split(","),
        "dpi": args.dpi,
        "zoom": args.zoom,
        "output": rendered,
        "board_bounds_mm": [bounds.x_min, bounds.y_min, bounds.x_max, bounds.y_max],
        "optional_tier": tier,
        "visual_guidance_only": True,
        "measurement_admissible": False,
    }, schema_version=SCHEMA_VERSION)
    write_report(report, args.json)
    emit(report)
    if report["summary"][FAIL]:
        return 1
    return 0 if rendered else 1


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
