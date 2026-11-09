"""Authentication: turn an `Authorization: Bearer <token>` header into an identity.

Tokens are mock credentials loaded from data/tokens.json. In production this
module would verify a signed JWT or call a token-introspection endpoint
instead - the rest of the gateway only depends on `Identity`.
"""

import json
from enum import Enum
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from mcp_gateway.jsonrpc import UNAUTHENTICATED, JsonRpcError

TOKENS_FILE = files("mcp_gateway") / "data" / "tokens.json"


class Role(str, Enum):
    ADMIN = "admin"
    VIEWER = "viewer"


class Identity(BaseModel):
    model_config = ConfigDict(frozen=True)

    user: str
    role: Role


class _TokenRecord(Identity):
    token: str


class TokenStore:
    def __init__(self, identities_by_token: dict[str, Identity]):
        self._identities_by_token = identities_by_token

    @classmethod
    def from_file(cls, path: Path = TOKENS_FILE) -> "TokenStore":
        data = json.loads(path.read_text(encoding="utf-8"))
        records = [_TokenRecord.model_validate(item) for item in data["tokens"]]
        return cls({r.token: Identity(user=r.user, role=r.role) for r in records})

    def identify(self, token: str) -> Identity | None:
        return self._identities_by_token.get(token)


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Return the token from 'Bearer <token>', or None if the header is missing or malformed."""
    if not authorization_header:
        return None

    scheme, _, token = authorization_header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def authenticate(authorization_header: str | None, token_store: TokenStore) -> Identity:
    """Resolve the caller's identity or raise a 401 JSON-RPC error."""
    token = extract_bearer_token(authorization_header)
    identity = token_store.identify(token) if token else None
    if identity is None:
        raise JsonRpcError(UNAUTHENTICATED, "Missing or invalid bearer token", http_status=401)
    return identity
