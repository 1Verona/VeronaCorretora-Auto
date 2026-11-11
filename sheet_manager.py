from __future__ import annotations

import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from evolution_client import phone_variants
from scraper import DEFAULT_CREDENTIALS_PATH, DEFAULT_SPREADSHEET_ID, SCOPES

import functools
import os
import threading
import time

import pytz

# Escritas na planilha: serializadas por lock (evita corrida entre disparo,
# webhook e bot) e com retry exponencial nativo do googleapiclient (429/5xx).
SHEETS_RETRIES = int(os.getenv("SHEETS_RETRIES", "4"))
# Cache curto de cabeçalhos/abas: evita 3-5 leituras repetidas por escrita.
# Curto de propósito: se alguém inserir coluna na mão, em <= TTL volta a ler.
SHEETS_HEADER_TTL = float(os.getenv("SHEETS_HEADER_TTL", "60"))
SHEETS_LIST_TTL = float(os.getenv("SHEETS_LIST_TTL", "300"))
TZ = pytz.timezone(os.getenv("TZ", "America/Sao_Paulo") or "America/Sao_Paulo")
_write_lock = threading.RLock()


def _now_str() -> str:
    return datetime.now(TZ).strftime("%d/%m/%Y %H:%M")


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _write_lock:
            return fn(*args, **kwargs)
    return wrapper


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
        self._headers_cache: dict[str, tuple[float, list[str]]] = {}
        self._sheets_cache: tuple[float, list[str]] | None = None
        self._cache_lock = threading.Lock()

    def invalidate_cache(self, sheet_name: str | None = None) -> None:
        with self._cache_lock:
            if sheet_name is None:
                self._headers_cache.clear()
                self._sheets_cache = None
            else:
                self._headers_cache.pop(sheet_name, None)

    @property
    def service(self):
        if self._service is None:
            credentials = Credentials.from_service_account_file(self.credentials_path, scopes=SCOPES)
            self._service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        return self._service

    def list_sheets(self, fresh: bool = False) -> list[str]:
        with self._cache_lock:
            cached = self._sheets_cache
        if not fresh and cached and time.time() - cached[0] < SHEETS_LIST_TTL:
            return list(cached[1])
        metadata = self.service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets.properties.title",
        ).execute(num_retries=SHEETS_RETRIES)
        titles = [s["properties"]["title"] for s in metadata.get("sheets", [])]
        with self._cache_lock:
            self._sheets_cache = (time.time(), titles)
        return list(titles)

    def _rows_from_values(self, sheet_name: str, values: list[list[str]]) -> list[dict[str, Any]]:
        if not values:
            return []
        headers = [h.lower().strip() for h in values[0]]
        with self._cache_lock:
            self._headers_cache[sheet_name] = (time.time(), headers)
        rows = []
        for i, row in enumerate(values[1:], start=2):
            row_dict = {"_row": i, "_sheet": sheet_name}
            for j, header in enumerate(headers):
                row_dict[header] = row[j] if j < len(row) else ""
            rows.append(row_dict)
        return rows

    def iter_all_rows(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """Lê todas as abas numa única chamada (values.batchGet) em vez de 1 por aba."""
        sheets = self.list_sheets()
        if not sheets:
            return []
        result = self.service.spreadsheets().values().batchGet(
            spreadsheetId=self.spreadsheet_id,
            ranges=sheets,
        ).execute(num_retries=SHEETS_RETRIES)
        out = []
        for sheet, vr in zip(sheets, result.get("valueRanges", [])):
            out.append((sheet, self._rows_from_values(sheet, vr.get("values", []))))
        return out

    def get_all_rows_from_sheet(self, sheet_name: str) -> list[dict[str, Any]]:
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=sheet_name,
        ).execute(num_retries=SHEETS_RETRIES)
        return self._rows_from_values(sheet_name, result.get("values", []))

    def find_client(self, name: str, *, threshold: float = 0.6, max_results: int = 5) -> list[dict[str, Any]]:
        name_lower = name.lower().strip()
        name_parts = set(name_lower.split())
        results: list[dict[str, Any]] = []

        for sheet, rows in self.iter_all_rows():
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

    def _headers(self, sheet_name: str, fresh: bool = False) -> list[str]:
        """Cabeçalho (minúsculo) com cache curto (SHEETS_HEADER_TTL)."""
        if not fresh and SHEETS_HEADER_TTL > 0:
            with self._cache_lock:
                cached = self._headers_cache.get(sheet_name)
            if cached and time.time() - cached[0] < SHEETS_HEADER_TTL:
                return list(cached[1])
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{sheet_name}!A1:ZZ1",
        ).execute(num_retries=SHEETS_RETRIES)
        headers = [h.lower().strip() for h in (result.get("values", [[]]) or [[]])[0]]
        with self._cache_lock:
            self._headers_cache[sheet_name] = (time.time(), headers)
        return list(headers)

    @_serialized
    def ensure_columns(self, sheet_name: str, required: list[str]) -> list[str]:
        """Garante as colunas e devolve o cabeçalho atualizado (minúsculo)."""
        existing = self._headers(sheet_name)
        to_add = [c for c in required if c.lower() not in existing]
        if to_add:
            # antes de criar coluna, confirma com leitura fresca (cache pode estar velho)
            existing = self._headers(sheet_name, fresh=True)
            to_add = [c for c in required if c.lower() not in existing]
        if not to_add:
            return existing

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
        ).execute(num_retries=SHEETS_RETRIES)
        headers = existing + [c.lower().strip() for c in to_add]
        with self._cache_lock:
            self._headers_cache[sheet_name] = (time.time(), headers)
        return list(headers)

    @_serialized
    def _write_row(
        self,
        sheet_name: str,
        row: int,
        fields: dict[str, str] | None = None,
        *,
        status: str = "",
        note: str = "",
        follow_up_date: str = "",
        extra_columns: list[str] | None = None,
    ) -> None:
        """Escreve campos + status/nota/follow-up numa única batchUpdate.

        Antes: cada mark_* fazia ~7 chamadas (3-5 leituras de cabeçalho repetidas,
        2 batchUpdate). Agora: cabeçalho em cache + 1 leitura da nota (se houver) + 1 escrita.
        """
        required = list(extra_columns or [])
        if status or note or follow_up_date:
            required += ["Status", "Notas", "Follow-up"]
        headers = self.ensure_columns(sheet_name, required) if required else self._headers(sheet_name)
        updates = []

        def put(col_name: str, value: str) -> None:
            key = col_name.lower().strip()
            if key in headers:
                letter = self._col_index_to_letter(headers.index(key) + 1)
                updates.append({"range": f"{sheet_name}!{letter}{row}", "values": [[value]]})

        for name, value in (fields or {}).items():
            put(name, value)
        now = _now_str()
        if status:
            put("Status", f"{status} ({now})")
        if note and "notas" in headers:
            letter = self._col_index_to_letter(headers.index("notas") + 1)
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{sheet_name}!{letter}{row}",
            ).execute(num_retries=SHEETS_RETRIES)
            current = (result.get("values", [[]])[0][0] if result.get("values") else "")
            new_note = f"[{now}] {note}"
            put("Notas", f"{current}\n{new_note}".strip() if current else new_note)
        if follow_up_date:
            put("Follow-up", follow_up_date)
        if updates:
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute(num_retries=SHEETS_RETRIES)

    @_serialized
    def update_client_status(self, sheet_name: str, row: int, status: str, notes: str = "", follow_up_date: str = "") -> dict[str, Any]:
        self._write_row(sheet_name, row, status=status, note=notes, follow_up_date=follow_up_date)
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
        now = datetime.now(TZ).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
        if when == "today":
            start, end = now, now + timedelta(days=1)
        elif when == "week":
            start, end = now, now + timedelta(days=7)
        elif when == "overdue":
            start, end = datetime(1970, 1, 1), now
        else:
            start, end = now, now + timedelta(days=7)

        results: list[dict[str, Any]] = []
        for sheet, rows in self.iter_all_rows():
            for row in rows:
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
        now = datetime.now(TZ).replace(tzinfo=None)
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

        for sheet, rows in self.iter_all_rows():
            for row in rows:
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
        return self._headers(sheet_name)

    @_serialized
    def _batch_update_row(self, sheet_name: str, row: int, fields: dict[str, str]) -> None:
        self._write_row(sheet_name, row, fields)

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
                # Linha já tratada (bloqueada/sem interesse/duplicada): antes continuava
                # "pendente", era remarcada a cada ciclo (nota crescendo) e ocupava a fila.
                if (row.get("estagio_conversa", "") or "").strip():
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

    @_serialized
    def mark_first_contact(self, sheet_name: str, row: int, when: datetime, template_used: str = "", jid: str = "") -> None:
        fields = {
            "Primeiro_contato_em": when.strftime("%d/%m/%Y %H:%M"),
            "Estagio_conversa": "ENGAGED",
        }
        if jid:
            fields["Evolution_jid"] = jid
        self._write_row(
            sheet_name, row, fields,
            status="PRIMEIRO CONTATO ENVIADO 📤" if template_used else "",
            note=f"Template usado: {template_used[:80]}" if template_used else "",
            extra_columns=OUTREACH_COLUMNS,
        )

    @_serialized
    def mark_duplicate_contact(self, sheet_name: str, row: int, reason: str = "") -> None:
        """Mesmo telefone já recebeu a 1ª mensagem por outra linha/aba: não reenvia."""
        self._write_row(
            sheet_name, row, {"Estagio_conversa": "DUPLICADO"},
            note=reason or "Telefone já contatado por outra linha — 1ª mensagem não reenviada",
            extra_columns=OUTREACH_COLUMNS,
        )

    @_serialized
    def update_stage(self, sheet_name: str, row: int, stage: str) -> None:
        self._write_row(sheet_name, row, {"Estagio_conversa": stage}, extra_columns=OUTREACH_COLUMNS)

    @_serialized
    def mark_meeting_scheduled(
        self,
        sheet_name: str,
        row: int,
        when: datetime,
        meet_link: str = "",
        notes: str = "",
    ) -> None:
        timestamp = when.strftime("%d/%m/%Y %H:%M")
        fields = {
            "Reuniao_em": timestamp,
            "Estagio_conversa": "SCHEDULED",
        }
        if meet_link:
            fields["Reuniao_link"] = meet_link
        note_text = notes or f"Reunião agendada para {timestamp}"
        if meet_link:
            note_text = f"{note_text} — {meet_link}"
        self._write_row(
            sheet_name, row, fields,
            status="REUNIÃO AGENDADA 📋",
            note=note_text,
            follow_up_date=when.strftime("%d/%m/%Y"),
            extra_columns=OUTREACH_COLUMNS,
        )

    @_serialized
    def mark_not_interested(self, sheet_name: str, row: int, reason: str = "") -> None:
        self._write_row(
            sheet_name, row, {"Estagio_conversa": "NOT_INTERESTED"},
            status="SEM INTERESSE ❌",
            note=reason or "Lead encerrou conversa sem interesse",
            extra_columns=OUTREACH_COLUMNS,
        )

    def find_lead_by_phone(self, phone: str) -> dict[str, Any] | None:
        targets = phone_variants(phone)
        if not targets:
            return None
        for sheet, rows in self.iter_all_rows():
            for row in rows:
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

    @_serialized
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
            sheets = self.list_sheets(fresh=True)
        if sheet_name not in sheets:
            return {"added": False, "error": f"Aba '{sheet_name}' não existe. Abas: {sheets}"}

        headers = self._headers(sheet_name, fresh=True)
        if not headers:
            return {"added": False, "error": f"Aba '{sheet_name}' sem cabeçalho."}

        headers = self.ensure_columns(sheet_name, ["Status", "Notas", "Follow-up"])

        now = _now_str()
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
        ).execute(num_retries=SHEETS_RETRIES)

        return {"added": True, "sheet": sheet_name, "nome": nome}
