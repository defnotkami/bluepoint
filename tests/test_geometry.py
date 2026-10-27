import math

import pytest

from geometry import (
    convert,
    format_area,
    format_length,
    nearest_point,
    parse_positive,
    polygon_area,
    polygon_perimeter,
    polyline_length,
    snap_angle,
)


def test_convert_round_trip():
    assert convert(1, "feet", "inches") == pytest.approx(12)
    assert convert(1, "meters", "millimeters") == pytest.approx(1000)
    assert convert(convert(3.7, "yards", "centimeters"), "centimeters", "yards") == pytest.approx(3.7)


def test_lengths_and_areas():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert polyline_length(square) == 30
    assert polygon_perimeter(square) == 40
    assert polygon_area(square) == 100
    # Orientation must not matter.
    assert polygon_area(list(reversed(square))) == 100
    assert polygon_area([(0, 0), (4, 0), (0, 3)]) == 6


def test_feet_and_inches_format():
    assert format_length(0.3048, "feet & inches") == "1' 0\""
    assert format_length(0.3048 * 12 + 0.0254 * 3.5, "feet & inches") == "12' 3 1/2\""
    assert format_length(1.5, "meters") == "1.50 m"
    assert format_area(1.0, "centimeters") == "10,000.00 cm²"
    assert format_area(0.09290304, "feet & inches") == "1.00 ft²"


def test_snap_angle():
    x, y = snap_angle((0, 0), (10, 1))
    assert (x, y) == pytest.approx((10, 0))
    x, y = snap_angle((0, 0), (1, 10))
    assert (x, y) == pytest.approx((0, 10), abs=1e-9)
    x, y = snap_angle((0, 0), (10, 9))
    assert math.atan2(y, x) == pytest.approx(math.pi / 4)


def test_nearest_point():
    pts = [(0, 0), (5, 5), (100, 100)]
    assert nearest_point((4, 4), pts, 3) == (5, 5)
    assert nearest_point((50, 50), pts, 3) is None


@pytest.mark.parametrize(
    "text, expected",
    [("2.5", 2.5), ("2,5", 2.5), ("1:100", 100), ("3/8", 0.375), (" 12 ", 12),
     ("0", None), ("-1", None), ("abc", None), ("", None), ("1/0", None), ("nan", None)],
)
def test_parse_positive(text, expected):
    assert parse_positive(text) == (pytest.approx(expected) if expected else None)
