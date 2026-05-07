from __future__ import annotations

import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from scraper import DEFAULT_CREDENTIALS_PATH, DEFAULT_SPREADSHEET_ID, SCOPES


class SheetManager:
    def __init__(self, credentials_path: Path = DEFAULT_CREDENTIALS_PATH, spreadsheet_id: str = DEFAULT_SPREADSHEET_ID) -> None:
        self.credentials_path = credentials_path
        self.spreadsheet_id = spreadsheet_id
        self._service = None

    @property
    def service(self):
        if self._service is None:
            credentials = Credentials.from_service_account_file(self.credentials_path, scopes=SCOPES)
            self._service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        return self._service

    def list_sheets(self) -> list[str]:
        metadata = self.service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets.properties.title",
        ).execute()
        return [s["properties"]["title"] for s in metadata.get("sheets", [])]

    def get_all_rows_from_sheet(self, sheet_name: str) -> list[dict[str, Any]]:
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=sheet_name,
        ).execute()
        values = result.get("values", [])
        if not values:
            return []

        headers = [h.lower().strip() for h in values[0]]
        rows = []
        for i, row in enumerate(values[1:], start=2):
            row_dict = {"_row": i, "_sheet": sheet_name}
            for j, header in enumerate(headers):
                row_dict[header] = row[j] if j < len(row) else ""
            rows.append(row_dict)
        return rows

    def find_client(self, name: str, *, threshold: float = 0.6, max_results: int = 5) -> list[dict[str, Any]]:
        name_lower = name.lower().strip()
        name_parts = set(name_lower.split())
        sheets = self.list_sheets()
        results: list[dict[str, Any]] = []

        for sheet in sheets:
            rows = self.get_all_rows_from_sheet(sheet)
            for row in rows:
                row_name = (row.get("nome", "") or "").lower().strip()
                if not row_name:
                    continue

                ratio = SequenceMatcher(None, name_lower, row_name).ratio()

                name_parts_row = set(row_name.split())
                overlap = len(name_parts & name_parts_row)
                total = len(name_parts | name_parts_row)
                jaccard = overlap / total if total > 0 else 0

                combined = max(ratio, jaccard)

                if combined >= threshold:
                    results.append({
                        "sheet": sheet,
                        "row": row["_row"],
                        "nome": row.get("nome", ""),
                        "score": round(combined, 3),
                        "telefone": row.get("telefone", row.get("telefone_planilha", "")),
                        "status_atual": row.get("status", ""),
                        "notas": row.get("notas", ""),
                    })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:max_results]

    def ensure_columns(self, sheet_name: str, required: list[str]) -> None:
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{sheet_name}!A1:ZZ1",
        ).execute()
        values = result.get("values", [])
        existing = [h.lower().strip() for h in (values[0] if values else [])]

        to_add = [c for c in required if c.lower() not in existing]
        if not to_add:
            return

        start_col = len(existing) + 1
        col_letter = self._col_index_to_letter(start_col)
        update_range = f"{sheet_name}!{col_letter}1:{col_letter}1"

        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=update_range,
            valueInputOption="RAW",
            body={"values": [to_add]},
        ).execute()

    def update_client_status(self, sheet_name: str, row: int, status: str, notes: str = "", follow_up_date: str = "") -> dict[str, Any]:
        self.ensure_columns(sheet_name, ["Status", "Notas", "Follow-up"])

        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{sheet_name}!A1:ZZ1",
        ).execute()
        headers = [h.lower().strip() for h in (result.get("values", [[]])[0])]

        status_col = headers.index("status") + 1 if "status" in headers else None
        notas_col = headers.index("notas") + 1 if "notas" in headers else None
        followup_col = headers.index("follow-up") + 1 if "follow-up" in headers else None

        updates = []
        now = datetime.now().strftime("%d/%m/%Y %H:%M")

        if status_col:
            col_letter = self._col_index_to_letter(status_col)
            updates.append({
                "range": f"{sheet_name}!{col_letter}{row}",
                "values": [[f"{status} ({now})"]],
            })

        if notas_col:
            col_letter = self._col_index_to_letter(notas_col)
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{sheet_name}!{col_letter}{row}",
            ).execute()
            current_notes = (result.get("values", [[]])[0][0] if result.get("values") else "")
            new_note = f"[{now}] {notes}" if notes else ""
            combined = f"{current_notes}\n{new_note}".strip() if current_notes else new_note
            updates.append({
                "range": f"{sheet_name}!{col_letter}{row}",
                "values": [[combined]],
            })

        if followup_col and follow_up_date:
            col_letter = self._col_index_to_letter(followup_col)
            updates.append({
                "range": f"{sheet_name}!{col_letter}{row}",
                "values": [[follow_up_date]],
            })

        if updates:
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()

        return {"updated": True, "sheet": sheet_name, "row": row, "status": status}

    def _col_index_to_letter(self, col_index: int) -> str:
        result = ""
        while col_index > 0:
            col_index, remainder = divmod(col_index - 1, 26)
            result = chr(65 + remainder) + result
        return result
