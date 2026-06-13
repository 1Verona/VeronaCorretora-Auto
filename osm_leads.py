#!/usr/bin/env python3
"""Gera leads de escritórios de advocacia a partir do OpenStreetMap (Overpass API).

Fonte complementar à base de CNPJ: não tem dado de "tempo de atuação", mas não
precisa de chave de API e captura estabelecimentos que tenham telefone cadastrado
no OSM e não apareçam bem na base de CNPJ.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any, Callable

import requests

from leads_sheet_writer import write_leads_sheet
from scraper import (
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_SPREADSHEET_ID,
    build_sheets_service,
    normalize_spreadsheet_id,
)

DEFAULT_OUTPUT_SHEET = "Leads_OSM_SC"
DEFAULT_AREA = "BR-SC"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
REQUEST_HEADERS = {"User-Agent": "VeronaCorretoraLeads/1.0 (contato via WhatsApp Business)"}

HEADERS = [
    "Status",
    "Nome",
    "Telefone",
    "Email",
    "Endereço",
    "Município",
    "CNAE",
    "Data Início Atividade",
    "Anos Atuação",
    "Prioridade",
    "ID",
]

NON_DIGIT_RE = re.compile(r"[^\d]")


def default_logger(message: str) -> None:
    print(message, flush=True)


def log(log_callback: Callable[[str], None] | None, message: str) -> None:
    (log_callback or default_logger)(message)


def build_query(area_code: str) -> str:
    return (
        "[out:json][timeout:180];\n"
        f'area["ISO3166-2"="{area_code}"]["admin_level"="4"]->.a;\n'
        '(\n'
        '  nwr["office"="lawyer"](area.a);\n'
        ');\n'
        "out center tags;"
    )


def fetch_osm_leads(area_code: str, log_callback: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    query = build_query(area_code)
    log(log_callback, f"Consultando Overpass API para área {area_code}...")
    resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=200, headers=REQUEST_HEADERS)
    resp.raise_for_status()
    elements = resp.json().get("elements", [])
    log(log_callback, f"{len(elements)} estabelecimento(s) retornado(s) pelo Overpass")
    return elements


def extract_phone(tags: dict[str, str]) -> str:
    for key in ("phone", "contact:phone", "contact:mobile"):
        value = (tags.get(key) or "").strip()
        if value:
            return value
    return ""


def extract_email(tags: dict[str, str]) -> str:
    return (tags.get("email") or tags.get("contact:email") or "").strip()


def extract_address(tags: dict[str, str]) -> str:
    parts: list[str] = []
    street = (tags.get("addr:street") or "").strip()
    number = (tags.get("addr:housenumber") or "").strip()
    if street:
        parts.append(f"{street}, {number}" if number else street)

    neighbourhood = (tags.get("addr:suburb") or tags.get("addr:neighbourhood") or "").strip()
    if neighbourhood:
        parts.append(neighbourhood)

    postcode = (tags.get("addr:postcode") or "").strip()
    if postcode:
        parts.append(postcode)

    return " - ".join(parts)


def build_lead_rows(elements: list[dict[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    seen: set[str] = set()

    for element in elements:
        tags = element.get("tags", {}) or {}
        nome = (tags.get("name") or "").strip()
        telefone = extract_phone(tags)
        if not nome or not telefone:
            continue

        dedup_key = f"{nome.lower()}|{NON_DIGIT_RE.sub('', telefone)}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        rows.append(
            [
                "",
                nome,
                telefone,
                extract_email(tags),
                extract_address(tags),
                (tags.get("addr:city") or "").strip(),
                "",
                "",
                "",
                "",
                dedup_key,
            ]
        )

    rows.sort(key=lambda r: r[1])
    return rows


def run(args: argparse.Namespace) -> None:
    elements = fetch_osm_leads(args.area, log_callback=print)
    rows = build_lead_rows(elements)
    print(f"Leads com nome e telefone: {len(rows)}")

    if args.limit:
        rows = rows[: args.limit]
        print(f"Aplicando limite manual: {len(rows)} registro(s)")

    service = build_sheets_service(args.credentials)
    result = write_leads_sheet(service, args.spreadsheet_id, args.output_sheet, HEADERS, rows, "ID")
    print(f"Aba '{args.output_sheet}': {result['added']} novo(s), {result['skipped']} já existente(s) (total processado: {result['total_input']})")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gera leads de escritórios de advocacia a partir do OpenStreetMap (Overpass API)")
    parser.add_argument("--area", default=DEFAULT_AREA, help="Código ISO3166-2 da área (default: BR-SC)")
    parser.add_argument("--spreadsheet-id", default=DEFAULT_SPREADSHEET_ID)
    parser.add_argument("--output-sheet", default=DEFAULT_OUTPUT_SHEET)
    parser.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS_PATH))
    parser.add_argument("--limit", type=int, default=None, help="Limite de leads escritos (para testes)")

    args = parser.parse_args(argv)
    args.credentials = Path(args.credentials).expanduser().resolve()
    args.spreadsheet_id = normalize_spreadsheet_id(args.spreadsheet_id)
    return args


def main() -> None:
    args = parse_args()
    try:
        run(args)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
