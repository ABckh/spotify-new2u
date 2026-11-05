"""The app's field list is the constant the database tests check."""

from fastapi.testclient import TestClient

from app.privacy.stored_fields import STORED_FIELDS, Category


def test_stored_fields_endpoint_returns_the_constant(client: TestClient) -> None:
    response = client.get("/v1/privacy/stored-fields")

    assert response.status_code == 200
    body = response.json()
    assert body["categories"] == [c.value for c in Category]
    assert [(f["table"], f["column"], f["category"], f["label"]) for f in body["fields"]] == [
        (f.table, f.column, f.category.value, f.label) for f in STORED_FIELDS
    ]
    assert body["provider_extras"] == [
        {
            "source": "spotify",
            "key": "spotify_album_artists",
            "category": "track_details",
            "label": "The album's artists",
        },
        {
            "source": "spotify",
            "key": "spotify_album_total_tracks",
            "category": "track_details",
            "label": "How many tracks the album has",
        },
        {
            "source": "spotify",
            "key": "spotify_album_type",
            "category": "track_details",
            "label": "Whether Spotify calls the release an album, a single or a compilation",
        },
    ]
    assert all("search_tsv" != f["column"] for f in body["fields"])
