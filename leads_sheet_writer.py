from __future__ import annotations

from typing import Any


def ensure_sheet_exists(service, spreadsheet_id: str, sheet_name: str) -> None:
    metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = [sheet["properties"]["title"] for sheet in metadata.get("sheets", [])]
    if sheet_name in existing:
        return

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
    ).execute()


def write_leads_sheet(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    rows: list[list[str]],
    dedup_key_column: str,
) -> dict[str, Any]:
    """Garante que a aba exista (com cabeçalho) e adiciona apenas as linhas cuja
    chave de deduplicação ainda não está presente, preservando o trabalho já feito
    pelo corretor em linhas existentes."""
    ensure_sheet_exists(service, spreadsheet_id, sheet_name)

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=sheet_name,
    ).execute()
    existing_values = result.get("values", [])

    key_index = headers.index(dedup_key_column)

    if not existing_values:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()
        existing_keys: set[str] = set()
    else:
        existing_headers = existing_values[0]
        try:
            existing_key_index = existing_headers.index(dedup_key_column)
        except ValueError:
            existing_key_index = key_index
        existing_keys = {
            row[existing_key_index].strip()
            for row in existing_values[1:]
            if len(row) > existing_key_index and row[existing_key_index].strip()
        }

    new_rows = [row for row in rows if row[key_index] not in existing_keys]

    if new_rows:
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A:A",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()

    return {"added": len(new_rows), "skipped": len(rows) - len(new_rows), "total_input": len(rows)}
