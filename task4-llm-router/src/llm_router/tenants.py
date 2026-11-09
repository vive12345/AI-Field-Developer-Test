"""Tenant lookup: `Authorization: Bearer <tenant API key>` -> Tenant (id + token budget).

Tenants are mock data loaded from data/tenants.json.
"""

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from llm_router import errors

TENANTS_FILE = files("llm_router") / "data" / "tenants.json"


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    tokens_per_minute: int


class TenantStore:
    def __init__(self, tenants_by_key: dict[str, Tenant]):
        self._tenants_by_key = tenants_by_key

    @classmethod
    def from_file(cls, path: Path = TENANTS_FILE) -> "TenantStore":
        records = json.loads(path.read_text(encoding="utf-8"))["tenants"]
        return cls({r["api_key"]: Tenant(r["tenant_id"], int(r["tokens_per_minute"])) for r in records})

    def authenticate(self, authorization_header: str | None) -> Tenant:
        scheme, _, api_key = (authorization_header or "").partition(" ")
        tenant = self._tenants_by_key.get(api_key.strip()) if scheme.lower() == "bearer" else None
        if tenant is None:
            raise errors.invalid_api_key()
        return tenant
