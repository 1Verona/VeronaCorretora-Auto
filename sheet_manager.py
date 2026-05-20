from __future__ import annotations

import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from evolution_client import normalize_phone, phone_variants
from scraper import DEFAULT_CREDENTIALS_PATH, DEFAULT_SPREADSHEET_ID, SCOPES

OUTREACH_COLUMNS = ["Primeiro_contato_em", "Estagio_conversa", "Evolution_jid", "Reuniao_em", "Reuniao_link"]

STATUS_KEYWORDS = {
    "fechado": ["fechado", "fechou", "contratou", "assinou"],
    "sem_interesse": ["sem interesse", "recusou", "dispensou"],
    "em_negociacao": ["negociação", "negociacao", "analisando"],
    "agendar_retorno": ["agendar retorno", "retornar", "ligar"],
    "reuniao": ["reunião", "reuniao", "visita"],
    "proposta": ["enviar proposta", "proposta", "orçamento"],
}


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
        end_col = start_col + len(to_add) - 1
        start_letter = self._col_index_to_letter(start_col)
        end_letter = self._col_index_to_letter(end_col)
        update_range = f"{sheet_name}!{start_letter}1:{end_letter}1"

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

    def get_client_details(self, name: str) -> dict[str, Any]:
        candidates = self.find_client(name, threshold=0.5, max_results=3)
        if not candidates:
            return {"found": False, "candidates": []}
        best = candidates[0]
        rows = self.get_all_rows_from_sheet(best["sheet"])
        full_row = next((r for r in rows if r["_row"] == best["row"]), None)
        return {
            "found": True,
            "candidates": candidates,
            "best": best,
            "full_row": full_row,
        }

    def _parse_followup_date(self, raw: str) -> datetime | None:
        if not raw:
            return None
        raw = raw.strip().split()[0]
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

    def list_followups(self, when: str = "today") -> list[dict[str, Any]]:
        now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if when == "today":
            start, end = now, now + timedelta(days=1)
        elif when == "week":
            start, end = now, now + timedelta(days=7)
        elif when == "overdue":
            start, end = datetime(1970, 1, 1), now
        else:
            start, end = now, now + timedelta(days=7)

        results: list[dict[str, Any]] = []
        for sheet in self.list_sheets():
            for row in self.get_all_rows_from_sheet(sheet):
                followup_raw = row.get("follow-up", "") or row.get("follow up", "") or row.get("followup", "")
                if not followup_raw:
                    continue
                dt = self._parse_followup_date(str(followup_raw))
                if not dt:
                    continue
                if start <= dt < end:
                    results.append({
                        "sheet": sheet,
                        "row": row["_row"],
                        "nome": row.get("nome", ""),
                        "telefone": row.get("telefone", "") or row.get("telefone_planilha", ""),
                        "status": row.get("status", ""),
                        "follow_up": followup_raw,
                        "follow_up_date": dt,
                        "notas": row.get("notas", ""),
                    })
        results.sort(key=lambda r: r["follow_up_date"])
        return results

    def get_stats(self, period: str = "month") -> dict[str, Any]:
        now = datetime.now()
        if period == "month":
            cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            cutoff = now - timedelta(days=7)
        elif period == "day":
            cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        status_counts: dict[str, int] = {}
        total = 0
        recent_status_counts: dict[str, int] = {}
        recent_total = 0
        status_ts_re = re.compile(r"\((\d{2}/\d{2}/\d{4})")

        for sheet in self.list_sheets():
            for row in self.get_all_rows_from_sheet(sheet):
                status_raw = (row.get("status", "") or "").strip()
                if not status_raw:
                    continue
                total += 1
                bucket = self._bucket_status(status_raw)
                status_counts[bucket] = status_counts.get(bucket, 0) + 1

                m = status_ts_re.search(status_raw)
                if m:
                    try:
                        ts = datetime.strptime(m.group(1), "%d/%m/%Y")
                        if ts >= cutoff:
                            recent_total += 1
                            recent_status_counts[bucket] = recent_status_counts.get(bucket, 0) + 1
                    except ValueError:
                        pass

        conversion = 0.0
        if recent_total > 0:
            conversion = recent_status_counts.get("fechado", 0) / recent_total

        return {
            "period": period,
            "cutoff": cutoff.isoformat(timespec="seconds"),
            "total": total,
            "by_status": status_counts,
            "recent_total": recent_total,
            "recent_by_status": recent_status_counts,
            "conversion_rate": round(conversion, 3),
        }

    def _bucket_status(self, raw: str) -> str:
        norm = raw.lower()
        for bucket, keywords in STATUS_KEYWORDS.items():
            for kw in keywords:
                if kw in norm:
                    return bucket
        return "outro"

    def _read_headers(self, sheet_name: str) -> list[str]:
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{sheet_name}!A1:ZZ1",
        ).execute()
        return [h.lower().strip() for h in (result.get("values", [[]])[0])]

    def _batch_update_row(self, sheet_name: str, row: int, fields: dict[str, str]) -> None:
        headers = self._read_headers(sheet_name)
        updates = []
        for header_name, value in fields.items():
            key = header_name.lower().strip()
            if key not in headers:
                continue
            col_index = headers.index(key) + 1
            col_letter = self._col_index_to_letter(col_index)
            updates.append({
                "range": f"{sheet_name}!{col_letter}{row}",
                "values": [[value]],
            })
        if updates:
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()

    def get_leads_pending_first_contact(
        self,
        sheet_name: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sheets = [sheet_name] if sheet_name else self.list_sheets()
        pending: list[dict[str, Any]] = []
        for sheet in sheets:
            try:
                self.ensure_columns(sheet, OUTREACH_COLUMNS)
            except HttpError:
                continue
            rows = self.get_all_rows_from_sheet(sheet)
            for row in rows:
                primeiro = (row.get("primeiro_contato_em", "") or "").strip()
                if primeiro:
                    continue
                telefone = (row.get("telefone", "") or row.get("telefone_planilha", "") or "").strip()
                if not telefone:
                    continue
                nome = (row.get("nome", "") or row.get("name", "") or "").strip()
                if not nome:
                    continue
                pending.append({
                    "sheet": sheet,
                    "row": row["_row"],
                    "nome": nome,
                    "telefone": telefone,
                    "email": (row.get("email", "") or row.get("email_planilha", "") or "").strip(),
                })
                if limit and len(pending) >= limit:
                    return pending
        return pending

    def mark_first_contact(self, sheet_name: str, row: int, when: datetime, template_used: str = "", jid: str = "") -> None:
        self.ensure_columns(sheet_name, OUTREACH_COLUMNS)
        timestamp = when.strftime("%d/%m/%Y %H:%M")
        fields = {
            "Primeiro_contato_em": timestamp,
            "Estagio_conversa": "ENGAGED",
        }
        if jid:
            fields["Evolution_jid"] = jid
        self._batch_update_row(sheet_name, row, fields)
        if template_used:
            self.update_client_status(
                sheet_name=sheet_name,
                row=row,
                status="PRIMEIRO CONTATO ENVIADO 📤",
                notes=f"Template usado: {template_used[:80]}",
            )

    def update_stage(self, sheet_name: str, row: int, stage: str) -> None:
        self.ensure_columns(sheet_name, OUTREACH_COLUMNS)
        self._batch_update_row(sheet_name, row, {"Estagio_conversa": stage})

    def mark_meeting_scheduled(
        self,
        sheet_name: str,
        row: int,
        when: datetime,
        meet_link: str = "",
        notes: str = "",
    ) -> None:
        self.ensure_columns(sheet_name, OUTREACH_COLUMNS)
        timestamp = when.strftime("%d/%m/%Y %H:%M")
        fields = {
            "Reuniao_em": timestamp,
            "Estagio_conversa": "SCHEDULED",
        }
        if meet_link:
            fields["Reuniao_link"] = meet_link
        self._batch_update_row(sheet_name, row, fields)
        note_text = notes or f"Reunião agendada para {timestamp}"
        if meet_link:
            note_text = f"{note_text} — {meet_link}"
        self.update_client_status(
            sheet_name=sheet_name,
            row=row,
            status="REUNIÃO AGENDADA 📋",
            notes=note_text,
            follow_up_date=when.strftime("%d/%m/%Y"),
        )

    def mark_not_interested(self, sheet_name: str, row: int, reason: str = "") -> None:
        self.ensure_columns(sheet_name, OUTREACH_COLUMNS)
        self._batch_update_row(sheet_name, row, {"Estagio_conversa": "NOT_INTERESTED"})
        self.update_client_status(
            sheet_name=sheet_name,
            row=row,
            status="SEM INTERESSE ❌",
            notes=reason or "Lead encerrou conversa sem interesse",
        )

    def find_lead_by_phone(self, phone: str) -> dict[str, Any] | None:
        targets = phone_variants(phone)
        if not targets:
            return None
        for sheet in self.list_sheets():
            for row in self.get_all_rows_from_sheet(sheet):
                row_variants = phone_variants(row.get("telefone", "") or row.get("telefone_planilha", ""))
                if row_variants & targets:
                    return {
                        "sheet": sheet,
                        "row": row["_row"],
                        "nome": (row.get("nome", "") or row.get("name", "")).strip(),
                        "telefone": next(iter(row_variants)),
                        "email": (row.get("email", "") or row.get("email_planilha", "")).strip(),
                        "estagio": (row.get("estagio_conversa", "") or "").strip(),
                    }
        return None

    def add_client(
        self,
        sheet_name: str,
        nome: str,
        telefone: str = "",
        email: str = "",
        notas: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        sheets = self.list_sheets()
        if sheet_name not in sheets:
            return {"added": False, "error": f"Aba '{sheet_name}' não existe. Abas: {sheets}"}

        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{sheet_name}!A1:ZZ1",
        ).execute()
        headers = [h.lower().strip() for h in (result.get("values", [[]])[0])]
        if not headers:
            return {"added": False, "error": f"Aba '{sheet_name}' sem cabeçalho."}

        self.ensure_columns(sheet_name, ["Status", "Notas", "Follow-up"])
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{sheet_name}!A1:ZZ1",
        ).execute()
        headers = [h.lower().strip() for h in (result.get("values", [[]])[0])]

        now = datetime.now().strftime("%d/%m/%Y %H:%M")
        row_values = [""] * len(headers)
        field_map = {
            "nome": nome,
            "name": nome,
            "telefone": telefone,
            "telefone_planilha": telefone,
            "email": email,
            "email_planilha": email,
            "status": f"{status} ({now})" if status else "",
            "notas": f"[{now}] {notas}" if notas else "",
        }
        for i, header in enumerate(headers):
            if header in field_map:
                row_values[i] = field_map[header]

        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"{sheet_name}!A:A",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row_values]},
        ).execute()

        return {"added": True, "sheet": sheet_name, "nome": nome}
