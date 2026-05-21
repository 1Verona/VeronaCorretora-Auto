from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "sheets_config.json"

DEFAULT_SPREADSHEET_ID = "1QRnMXp8lTmIefhHnP5LdSoin1QMj_imewcf3RAh_KzI"
DEFAULT_OUTPUT_SHEET = "Leads_OAB_Scraper"
DEFAULT_SECCIONAL = "Santa Catarina"

DEFAULT_CONFIG: dict[str, Any] = {
    "spreadsheet_id": DEFAULT_SPREADSHEET_ID,
    "output_sheet_name": DEFAULT_OUTPUT_SHEET,
    "seccional": DEFAULT_SECCIONAL,
    "source_sheet": "",
}

CONFIG_FIELDS = set(DEFAULT_CONFIG.keys())

_lock = threading.Lock()


def _merge(extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULT_CONFIG)
    for k, v in (extra or {}).items():
        if k in CONFIG_FIELDS:
            out[k] = (v or "").strip() if isinstance(v, str) else v
    if not out["spreadsheet_id"]:
        out["spreadsheet_id"] = DEFAULT_SPREADSHEET_ID
    if not out["output_sheet_name"]:
        out["output_sheet_name"] = DEFAULT_OUTPUT_SHEET
    if not out["seccional"]:
        out["seccional"] = DEFAULT_SECCIONAL
    return out


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        merged = _merge({})
        save_config(merged)
        return merged
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return _merge(data)


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = _merge(config or {})
    with _lock:
        CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def get_spreadsheet_id() -> str:
    return load_config()["spreadsheet_id"]


def get_output_sheet_name() -> str:
    return load_config()["output_sheet_name"]


def get_seccional() -> str:
    return load_config()["seccional"]
