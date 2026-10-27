"""Pure math helpers: units, lengths, areas and snapping. No UI code here."""

import math

# Conversion factors to meters.
UNIT_TO_METERS = {
    "millimeters": 0.001,
    "centimeters": 0.01,
    "meters": 1.0,
    "kilometers": 1000.0,
    "inches": 0.0254,
    "feet": 0.3048,
    "yards": 0.9144,
    "miles": 1609.344,
}

UNIT_ABBR = {
    "millimeters": "mm",
    "centimeters": "cm",
    "meters": "m",
    "kilometers": "km",
    "inches": "in",
    "feet": "ft",
    "yards": "yd",
    "miles": "mi",
    "feet & inches": "ft",
}

# Display units also offer architectural feet & inches (e.g. 12' 3 1/2").
DISPLAY_UNITS = list(UNIT_TO_METERS) + ["feet & inches"]

INCH = 0.0254


def convert(value, from_unit, to_unit):
    return value * UNIT_TO_METERS[from_unit] / UNIT_TO_METERS[to_unit]


def polyline_length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def polygon_perimeter(points):
    if len(points) < 2:
        return 0.0
    return polyline_length(points) + math.dist(points[-1], points[0])


def polygon_area(points):
    """Shoelace formula. Works for any simple (non self-crossing) polygon."""
    if len(points) < 3:
        return 0.0
    s = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def centroid(points):
    n = len(points)
    return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)


def _fmt_number(value, decimals):
    return f"{value:,.{decimals}f}"


def _feet_inches(meters, denominator=16):
    """Format as architectural feet and inches rounded to 1/denominator inch."""
    sign = "-" if meters < 0 else ""
    total = round(abs(meters) / INCH * denominator)  # in 1/denominator inches
    feet, rem = divmod(total, 12 * denominator)
    inches, frac = divmod(rem, denominator)
    text = f"{sign}{feet}' {inches}"
    if frac:
        g = math.gcd(frac, denominator)
        text += f" {frac // g}/{denominator // g}"
    return text + '"'


def format_length(meters, unit, decimals=2):
    if unit == "feet & inches":
        return _feet_inches(meters)
    value = meters / UNIT_TO_METERS[unit]
    return f"{_fmt_number(value, decimals)} {UNIT_ABBR[unit]}"


def format_area(square_meters, unit, decimals=2):
    base = "feet" if unit == "feet & inches" else unit
    value = square_meters / UNIT_TO_METERS[base] ** 2
    return f"{_fmt_number(value, decimals)} {UNIT_ABBR[base]}²"


def snap_angle(origin, point, step_degrees=45):
    """Move `point` so the segment from `origin` follows the nearest multiple
    of `step_degrees` (0°, 45°, 90°…), keeping the projected length."""
    dx, dy = point[0] - origin[0], point[1] - origin[1]
    if dx == 0 and dy == 0:
        return point
    step = math.radians(step_degrees)
    angle = round(math.atan2(dy, dx) / step) * step
    ux, uy = math.cos(angle), math.sin(angle)
    length = dx * ux + dy * uy
    return (origin[0] + ux * length, origin[1] + uy * length)


def nearest_point(point, candidates, radius):
    """Closest candidate within `radius` of `point`, or None."""
    best, best_d = None, radius
    for c in candidates:
        d = math.dist(point, c)
        if d <= best_d:
            best, best_d = c, d
    return best


def parse_positive(text):
    """Parse a user-typed positive number. Accepts commas, '1:100' and fractions
    such as '3/8'. Returns None when the text is not a positive number."""
    if text is None:
        return None
    s = str(text).strip().replace(",", ".").replace(" ", "")
    if ":" in s:  # "1:100" -> 100
        left, _, right = s.partition(":")
        try:
            a, b = float(left), float(right)
        except ValueError:
            return None
        return b / a if a > 0 and b > 0 else None
    if "/" in s:
        num, _, den = s.partition("/")
        try:
            v = float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    else:
        try:
            v = float(s)
        except ValueError:
            return None
    return v if v > 0 and math.isfinite(v) else None
