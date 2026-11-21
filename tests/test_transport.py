"""Branch locations: reading map links, distances, and the branches page."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import transport  # noqa: E402


@pytest.mark.parametrize("text, expected", [
    ("30.0444, 31.2357", (30.0444, 31.2357)),
    ("٣٠٫٠٤٤٤, ٣١٫٢٣٥٧", (30.0444, 31.2357)),
    ("https://www.google.com/maps/place/Pharmacy/@30.0500,31.2400,17z/data=!3m1!4b1!4m6!3m5!1s0x0:0x0!8m2!3d30.0444!4d31.2357", (30.0444, 31.2357)),
    ("https://www.google.com/maps/@24.7136,46.6753,15z", (24.7136, 46.6753)),
    ("https://maps.google.com/?q=21.4858,39.1925", (21.4858, 39.1925)),
    ("https://www.google.com/maps/search/?api=1&query=30.0131%2C31.2089", (30.0131, 31.2089)),
    ("https://www.google.com/maps/dir/?api=1&destination=29.9792,31.1342", (29.9792, 31.1342)),
])
def test_locations_are_read_from_links_and_text(text, expected):
    assert transport.parse_location(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "فرع المعادي", "https://maps.app.goo.gl/AbCdEf123", "0, 0", "120, 31", "https://example.com/page"])
def test_text_without_coordinates_gives_none(text):
    assert transport.parse_location(text) is None


def test_short_links_need_resolving_and_only_google_is_fetched():
    assert transport.needs_resolving("https://maps.app.goo.gl/AbCdEf123")
    assert not transport.needs_resolving("https://www.google.com/maps/@24.7,46.6,15z")
    # A host that is not Google comes back unchanged, without any request.
    assert transport.resolve_link("http://169.254.169.254/latest") == "http://169.254.169.254/latest"


def test_distances():
    cairo, alexandria = (30.0444, 31.2357), (31.2001, 29.9187)
    assert transport.haversine_km(cairo, alexandria) == pytest.approx(179, abs=3)
    matrix = transport.DistanceMatrix({"Cairo": cairo, "Alex": alexandria}, road_factor=1.3, speed_kmh=60)
    leg = matrix.leg("Cairo", "Alex")
    assert leg.km == pytest.approx(179 * 1.3, abs=4) and leg.minutes == pytest.approx(leg.km, abs=1) and not leg.measured
    assert matrix.leg("Alex", "Alex").km == 0
    # A measured pair wins and holds both ways unless the other way was measured too.
    measured = transport.DistanceMatrix({"Cairo": cairo, "Alex": alexandria}, [("Cairo", "Alex", 220, 170)])
    assert (measured.leg("Cairo", "Alex").km, measured.leg("Alex", "Cairo").minutes) == (220, 170)
    both = transport.DistanceMatrix({"Cairo": cairo, "Alex": alexandria}, [("Cairo", "Alex", 220, 170), ("Alex", "Cairo", 230, 190)])
    assert both.leg("Alex", "Cairo").km == 230


def test_directions_link_keeps_the_order():
    link = transport.directions_link([(30.0, 31.0), (30.1, 31.1), (30.2, 31.2)])
    assert "origin=30.000000,31.000000" in link and "destination=30.200000,31.200000" in link and "waypoints=30.100000,31.100000" in link
    assert transport.directions_link([(30.0, 31.0)]) == ""
