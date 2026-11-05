"""Privacy guarantees that are enforced by test rather than stated in copy.

- ``stored_fields``: the one constant the "what we collect" screen renders from.
- ``provider_extras``: the allowlist of keys permitted in ``unified_event.provider_extras``.
- ``audit``: ``pg_catalog`` queries the privacy tests use, so a new table
  or column is discovered from the live database instead of a hardcoded list.
- ``delete``: the single delete-everything path.
"""
