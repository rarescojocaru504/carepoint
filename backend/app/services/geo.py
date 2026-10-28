from geoalchemy2 import WKTElement

from app.schemas.common import GeoPoint

WGS84_SRID = 4326


def to_geography(point: GeoPoint) -> WKTElement:
    """Convert a validated point to a PostGIS geography literal.

    WKT is longitude-first: `POINT(lng lat)`.
    """
    return WKTElement(f"POINT({point.lng} {point.lat})", srid=WGS84_SRID)
