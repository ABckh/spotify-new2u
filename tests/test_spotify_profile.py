"""PRD P8 / S1.4a / B7: from the Spotify profile only the account ID and display name are kept."""

import dataclasses

import pytest

from app.privacy.stored_fields import SPOTIFY_PROFILE_COLUMNS, STORED_FIELDS, Category
from app.spotify.profile import SpotifyProfile, profile_columns, profile_from_me

# Shaped like GET /v1/me with every field Spotify can return. Synthetic values.
ME = {
    "id": "synthetic-account-1",
    "display_name": "Synthetic Name",
    "email": "synthetic@example.invalid",
    "country": "LT",
    "product": "premium",
    "followers": {"href": None, "total": 12},
    "images": [{"url": "https://i.example.invalid/synthetic.jpg", "height": 64, "width": 64}],
    "explicit_content": {"filter_enabled": False, "filter_locked": False},
    "external_urls": {"spotify": "https://open.example.invalid/user/synthetic-account-1"},
    "href": "https://api.example.invalid/v1/users/synthetic-account-1",
    "type": "user",
    "uri": "spotify:user:synthetic-account-1",
}


def test_s1_4a_field_list_shows_exactly_two_spotify_profile_fields() -> None:
    profile_fields = [
        f
        for f in STORED_FIELDS
        if f.table == "spotify_connection" and f.column in SPOTIFY_PROFILE_COLUMNS.values()
    ]

    assert sorted(SPOTIFY_PROFILE_COLUMNS) == ["display_name", "id"]
    assert [(f.column, f.category) for f in profile_fields] == [
        ("spotify_account_id", Category.CONNECTION),
        ("spotify_display_name", Category.CONNECTION),
    ]


def test_s1_4a_only_id_and_display_name_are_read_from_the_profile() -> None:
    profile = profile_from_me(ME)
    columns = profile_columns(profile)

    assert [f.name for f in dataclasses.fields(SpotifyProfile)] == ["account_id", "display_name"]
    assert profile == SpotifyProfile(
        account_id="synthetic-account-1", display_name="Synthetic Name"
    )
    assert columns == {
        "spotify_account_id": "synthetic-account-1",
        "spotify_display_name": "Synthetic Name",
    }
    for dropped in ("synthetic@example.invalid", "LT", "premium", "synthetic.jpg", "12"):
        assert dropped not in repr(columns)


def test_a_profile_without_a_display_name_keeps_the_account_id_only() -> None:
    for display_name in (None, ""):
        profile = profile_from_me({**ME, "display_name": display_name})

        assert profile == SpotifyProfile(account_id="synthetic-account-1", display_name=None)


@pytest.mark.parametrize("account_id", [None, "", 42])
def test_a_profile_without_a_usable_id_is_rejected_without_echoing_values(
    account_id: object,
) -> None:
    with pytest.raises(ValueError) as excinfo:
        profile_from_me({**ME, "id": account_id})

    assert "'id'" in str(excinfo.value)
    assert "Synthetic Name" not in str(excinfo.value)
