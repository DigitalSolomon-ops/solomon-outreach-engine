"""Secret lookup. Secret Manager in the cloud; env vars locally.

Local override: a secret named global-firecrawl is read from the env var
GLOBAL_FIRECRAWL if set. Values are never logged. list_names() reads secret
NAMES only (secretmanager.secrets.list, metadata), never values. The engine never
writes secrets: keys arrive through the Credentials surface, and deploy.ps1 creates
the unsubscribe signing key once.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

log = logging.getLogger(__name__)


def _env_name(secret: str) -> str:
    return secret.upper().replace("-", "_")


class Secrets:
    def __init__(self, project: str, use_secret_manager: bool = True):
        self.project = project
        self._client = None
        if use_secret_manager:
            try:
                from google.cloud import secretmanager  # imported lazily: optional locally
                self._client = secretmanager.SecretManagerServiceClient()
            except Exception as exc:  # no creds / lib locally is normal
                log.info("Secret Manager unavailable (%s); using env vars only", type(exc).__name__)

    @lru_cache(maxsize=128)
    def get(self, name: str | None) -> str | None:
        """Return the secret value, or None when it does not exist (provider is then skipped)."""
        if not name:
            return None
        env = os.environ.get(_env_name(name))
        if env:
            return env
        if self._client is None:
            return None
        from google.api_core import exceptions as gexc
        path = f"projects/{self.project}/secrets/{name}/versions/latest"
        try:
            resp = self._client.access_secret_version(name=path)
        except (gexc.NotFound, gexc.PermissionDenied, gexc.FailedPrecondition) as exc:
            log.info("secret %s not available (%s)", name, type(exc).__name__)
            return None
        return resp.payload.data.decode("utf-8").strip()

    def get_json(self, name: str | None) -> dict | None:
        raw = self.get(name)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"secret {name} is not valid JSON") from exc

    def list_names(self, known: list[str] | None = None) -> list[str]:
        """Every secret name in the project (cloud), or the known names set as env vars (local)."""
        if self._client is None:
            return [n for n in (known or []) if os.environ.get(_env_name(n))]
        parent = f"projects/{self.project}"
        names = [s.name.rsplit("/", 1)[-1] for s in self._client.list_secrets(request={"parent": parent})]
        names += [n for n in (known or []) if os.environ.get(_env_name(n)) and n not in names]
        return names
