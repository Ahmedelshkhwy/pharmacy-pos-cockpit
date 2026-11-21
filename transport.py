"""Branch locations and distances for planning transfer trips.

Locations come from a Google Maps link, a pin on the map, or plain "lat, lng" text. Distances are
the straight line between two branches times a road factor (city roads are longer than the straight
line), turned into minutes with an average speed. Any pair can be overridden with a measured
distance and time, which then wins.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlsplit

EARTH_RADIUS_KM = 6371.0088
SHORT_LINK_HOSTS = {"maps.app.goo.gl", "goo.gl", "g.co", "maps.google.com", "www.google.com", "google.com"}

_NUMBER = r"-?\d{1,3}(?:\.\d+)?"
_PAIR = re.compile(rf"({_NUMBER})\s*,\s*({_NUMBER})")


def valid(lat: float, lng: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lng <= 180 and not (lat == 0 and lng == 0)


def _pair(text: str) -> tuple[float, float] | None:
    match = _PAIR.search(text)
    if not match:
        return None
    lat, lng = float(match.group(1)), float(match.group(2))
    return (lat, lng) if valid(lat, lng) else None


def parse_location(text: str) -> tuple[float, float] | None:
    """Coordinates from a Google Maps link or "lat, lng" text. None when there are none in it.

    Short links (maps.app.goo.gl/...) hold no coordinates: resolve them first with resolve_link.
    """
    text = unquote((text or "").strip()).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789."))
    if not text:
        return None
    # The pin of a place: ...!3d30.0444!4d31.2357 (more exact than the map centre after "@").
    pin = re.search(rf"!3d({_NUMBER})!4d({_NUMBER})", text)
    if pin and valid(float(pin.group(1)), float(pin.group(2))):
        return float(pin.group(1)), float(pin.group(2))
    if text.startswith(("http://", "https://")):
        parts = urlsplit(text)
        query = parse_qs(parts.query)
        for key in ("q", "query", "ll", "center", "destination", "daddr"):
            for value in query.get(key, []):
                found = _pair(value)
                if found:
                    return found
        at = re.search(rf"@({_NUMBER}),({_NUMBER})", parts.path)
        if at and valid(float(at.group(1)), float(at.group(2))):
            return float(at.group(1)), float(at.group(2))
        found = _pair(parts.path.replace("/", " "))
        return found
    return _pair(text)


def needs_resolving(text: str) -> bool:
    host = urlsplit((text or "").strip()).hostname or ""
    return host in {"maps.app.goo.gl", "goo.gl", "g.co"}


def resolve_link(url: str, timeout: float = 6.0) -> str:
    """Follow a short Google Maps link to the full one. Only Google hosts are fetched."""
    import urllib.request

    host = urlsplit(url).hostname or ""
    if host not in SHORT_LINK_HOSTS:
        return url
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - host checked above
        final = response.geturl()
    # Some regions land on a consent page that carries the real link in "continue".
    continued = parse_qs(urlsplit(final).query).get("continue")
    return continued[0] if continued else final


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


@dataclass
class Leg:
    km: float
    minutes: float
    measured: bool = False      # True when a person entered this pair


class DistanceMatrix:
    """Road distance and driving time between every two located branches."""

    def __init__(self, locations: dict[str, tuple[float, float]], overrides: Iterable[tuple[str, str, float, float]] = (),
                 road_factor: float = 1.3, speed_kmh: float = 30.0) -> None:
        self.branches = sorted(locations)
        self.legs: dict[tuple[str, str], Leg] = {}
        for a in self.branches:
            for b in self.branches:
                if a != b:
                    km = haversine_km(locations[a], locations[b]) * road_factor
                    self.legs[(a, b)] = Leg(round(km, 1), round(km / speed_kmh * 60, 0) if speed_kmh > 0 else 0.0)
        for a, b, km, minutes in overrides:
            if a in locations and b in locations and a != b:
                # A measured pair holds both ways unless the other direction was measured too.
                self.legs[(a, b)] = Leg(round(km, 1), round(minutes, 0), True)
                if not any(o[0] == b and o[1] == a for o in overrides):
                    self.legs[(b, a)] = Leg(round(km, 1), round(minutes, 0), True)

    def leg(self, a: str, b: str) -> Leg:
        return Leg(0.0, 0.0) if a == b else self.legs[(a, b)]


def directions_link(points: list[tuple[float, float]]) -> str:
    """A Google Maps link that drives through the points in order (first is the start)."""
    if len(points) < 2:
        return ""
    fmt = lambda p: f"{p[0]:.6f},{p[1]:.6f}"  # noqa: E731
    link = f"https://www.google.com/maps/dir/?api=1&origin={fmt(points[0])}&destination={fmt(points[-1])}&travelmode=driving"
    if len(points) > 2:
        link += "&waypoints=" + "%7C".join(fmt(p) for p in points[1:-1])
    return link
