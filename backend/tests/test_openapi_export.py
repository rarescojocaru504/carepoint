"""The mobile app's API types are generated from mobile/openapi.json, so it must
match the running code. Regenerate it with `python -m app.scripts.export_openapi`."""

import json

import pytest

from app.scripts.export_openapi import DEFAULT_PATH, render


@pytest.mark.skipif(not DEFAULT_PATH.exists(), reason="mobile/ is not in the Docker image")
def test_published_openapi_matches_the_code() -> None:
    assert DEFAULT_PATH.read_text(encoding="utf-8") == render(), (
        "mobile/openapi.json is stale: run `python -m app.scripts.export_openapi` "
        "from backend/, then `npm run gen:api` in mobile/"
    )


def test_render_is_stable_json() -> None:
    first = render()
    assert first == render()
    assert first.endswith("\n")
    schema = json.loads(first)
    assert schema["openapi"].startswith("3.")
    # The endpoints the customer app is built on.
    for path in (
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/api/v1/me",
        "/api/v1/machines/nearby",
        "/api/v1/machines/{machine_id}",
        "/api/v1/reservations",
        "/api/v1/reservations/{reservation_id}/checkout",
        "/api/v1/orders",
    ):
        assert path in schema["paths"], path
