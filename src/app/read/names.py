"""Names as every read compares and serves them (R-31).

Its own module so ``app.read.album_sql`` and ``app.read.summary`` can both use
it without importing each other.
"""

from typing import Final


def normalized_name(column: str) -> str:
    """SQL for ``column`` after NFC and trimming whitespace, nothing else (R-31).

    [[:space:]] covers Unicode whitespace in UTF-8. "New to you" uses it too.
    """
    return f"regexp_replace(normalize({column}, NFC), '^[[:space:]]+|[[:space:]]+$', '', 'g')"


# An artist's name from one ``creators`` entry ``creator.raw``.
ARTIST_NAME: Final = normalized_name("creator.raw")
