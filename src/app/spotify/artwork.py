"""Album cover URLs (WHO-149): which ones we keep, and which image we pick.

Only Spotify's image server is accepted, on the way in (ingest, backfill) and on
the way out (read API). Anything else, including a malformed or oversized value,
is "no cover" (None), never an error: a cover is decoration, not history.

``i.scdn.co`` URLs are content-addressed (the path is a hash of the image), so
a stored URL stays valid in practice. One that stops working is shown as no
cover by the app.
"""

import re
from collections.abc import Mapping
from typing import Any

# Scheme, host and the /image/ path fixed (WHO-148 rules 1 and 10), then one
# plain path segment: no query, fragment, userinfo, port, percent-escapes,
# further slashes or whitespace. At most 225 characters in all (the rule: 256).
_SPOTIFY_IMAGE_URL = re.compile(r"https://i\.scdn\.co/image/[A-Za-z0-9._-]{1,200}")

# The smallest image at least this wide is picked: sharp in a list row, not a
# full-screen download.
MIN_WIDTH = 300


def safe_artwork_url(value: object) -> str | None:
    """``value`` if it is a Spotify image URL, else None."""
    if isinstance(value, str) and _SPOTIFY_IMAGE_URL.fullmatch(value):
        return value
    return None


def pick_album_image(album: Mapping[str, Any]) -> str | None:
    """The URL of the smallest ``album.images`` entry at least 300 wide, else of the widest.

    Images without a usable URL are ignored. When no image states a width,
    the first usable one is taken (Spotify lists the widest first).
    """
    images = album.get("images")
    if not isinstance(images, list):
        return None
    usable: list[tuple[int | None, str]] = []
    for image in images:
        if not isinstance(image, Mapping):
            continue
        url = safe_artwork_url(image.get("url"))
        if url is None:
            continue
        width = image.get("width")
        usable.append((width if isinstance(width, int) and width > 0 else None, url))
    if not usable:
        return None
    sized = [(w, url) for w, url in usable if w is not None]
    if not sized:
        return usable[0][1]
    wide_enough = [(w, url) for w, url in sized if w >= MIN_WIDTH]
    if wide_enough:
        return min(wide_enough, key=lambda pair: pair[0])[1]
    return max(sized, key=lambda pair: pair[0])[1]
