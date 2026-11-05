"""``GET /v1/privacy/stored-fields``: the exact field list for "what we collect".

Rendered straight from ``app.privacy.stored_fields.STORED_FIELDS`` and the
``provider_extras`` allowlist, so the app shows the same list the tests check
against the live database. It describes the schema, not any user's data, so it
holds nothing personal.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.privacy.provider_extras import PROVIDER_EXTRAS_ALLOWLIST
from app.privacy.stored_fields import STORED_FIELDS, Category

router = APIRouter(prefix="/v1/privacy", tags=["privacy"])


class StoredFieldOut(BaseModel):
    table: str
    column: str
    category: Category
    label: str


class ProviderExtrasKeyOut(BaseModel):
    source: str
    key: str
    category: Category
    label: str


class StoredFieldsResponse(BaseModel):
    categories: list[Category]
    fields: list[StoredFieldOut]
    provider_extras: list[ProviderExtrasKeyOut]


@router.get("/stored-fields", response_model=StoredFieldsResponse)
def stored_fields() -> StoredFieldsResponse:
    return StoredFieldsResponse(
        categories=list(Category),
        fields=[
            StoredFieldOut(table=f.table, column=f.column, category=f.category, label=f.label)
            for f in STORED_FIELDS
        ],
        provider_extras=[
            ProviderExtrasKeyOut(source=source, key=key, category=category, label=label)
            for source, keys in PROVIDER_EXTRAS_ALLOWLIST.items()
            for key, (category, label) in keys.items()
        ],
    )
