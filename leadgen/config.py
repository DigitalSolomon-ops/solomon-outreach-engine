"""Load system settings, model prices, and the icp.yaml seed. Campaigns live in Firestore."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_settings(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.environ.get("LEADGEN_SETTINGS", ROOT / "config" / "settings.yaml"))
    settings = _yaml(path)
    for key in ("rotation", "send", "budget", "writing", "replies"):
        if not isinstance(settings, dict) or key not in settings:
            raise ValueError(f"{path}: settings file has no {key!r} section")
    return settings


def load_models(path: str | os.PathLike | None = None) -> dict:
    """{'haiku': {id, input_per_mtok, output_per_mtok}, 'sonnet': {...}} from config/models.yaml."""
    path = Path(path or ROOT / "config" / "models.yaml")
    data = _yaml(path)
    models = data.get("models") or {}
    for tier in ("haiku", "sonnet"):
        m = models.get(tier) or {}
        if not m.get("id") or not isinstance(m.get("input_per_mtok"), (int, float)) \
                or not isinstance(m.get("output_per_mtok"), (int, float)):
            raise ValueError(f"{path}: models.{tier} needs id, input_per_mtok, output_per_mtok")
    return models


def load_icp_file(path: str | os.PathLike | None = None) -> dict:
    """The legacy lane file. Seed data only: it becomes the first campaign."""
    path = Path(path or ROOT / "config" / "icp.yaml")
    icp = _yaml(path)
    if not isinstance(icp, dict) or not icp.get("lanes"):
        raise ValueError("ICP has no 'lanes'")
    return icp
