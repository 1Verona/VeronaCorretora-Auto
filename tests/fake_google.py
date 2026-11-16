"""Fakes em memória das APIs Google (Sheets v4 / Calendar v3) com contador de chamadas.

Usado nos testes e na simulação (sim/simular_dia.py). Nenhuma chamada de rede.
"""
from __future__ import annotations

import re
import uuid
from collections import Counter
from typing import Any

_CELL = re.compile(r"^([A-Z]+)?(\d+)?$")


def col_to_idx(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _parse_range(rng: str) -> tuple[str, tuple[int, int, int | None, int | None]]:
    """'Aba!B2:C9' -> (aba, (r0, c0, r1, c1)) com índices 0-based; None = aberto."""
    if "!" in rng:
        sheet, a1 = rng.split("!", 1)
    else:
        sheet, a1 = rng, ""
    sheet = sheet.strip("'")
    if not a1:
        return sheet, (0, 0, None, None)
    start, _, end = a1.partition(":")
    m1 = _CELL.match(start)
    c0 = col_to_idx(m1.group(1)) if m1.group(1) else 0
    r0 = int(m1.group(2)) - 1 if m1.group(2) else 0
    if not end:
        return sheet, (r0, c0, r0, c0)
    m2 = _CELL.match(end)
    c1 = col_to_idx(m2.group(1)) if m2.group(1) else None
    r1 = int(m2.group(2)) - 1 if m2.group(2) else None
    return sheet, (r0, c0, r1, c1)


class _Req:
    def __init__(self, fn, counter: Counter, name: str):
        self._fn, self._counter, self._name = fn, counter, name

    def execute(self, num_retries: int = 0):
        self._counter[self._name] += 1
        self._counter["total"] += 1
        return self._fn()


class FakeSheetsService:
    def __init__(self, sheets: dict[str, list[list[str]]] | None = None):
        self.data: dict[str, list[list[str]]] = {k: [list(r) for r in v] for k, v in (sheets or {}).items()}
        self.calls: Counter = Counter()

    # -- helpers --
    def _grid(self, sheet: str) -> list[list[str]]:
        if sheet not in self.data:
            raise KeyError(f"aba inexistente: {sheet}")
        return self.data[sheet]

    def _write(self, rng: str, values: list[list[Any]]) -> None:
        sheet, (r0, c0, _r1, _c1) = _parse_range(rng)
        grid = self._grid(sheet)
        for i, row in enumerate(values):
            rr = r0 + i
            while len(grid) <= rr:
                grid.append([])
            for j, v in enumerate(row):
                cc = c0 + j
                while len(grid[rr]) <= cc:
                    grid[rr].append("")
                grid[rr][cc] = "" if v is None else str(v)

    def _read(self, rng: str) -> dict[str, Any]:
        sheet, (r0, c0, r1, c1) = _parse_range(rng)
        grid = self._grid(sheet)
        out = []
        last_r = len(grid) - 1 if r1 is None else min(r1, len(grid) - 1)
        for r in range(r0, last_r + 1):
            row = grid[r]
            end = len(row) - 1 if c1 is None else min(c1, len(row) - 1)
            vals = row[c0:end + 1]
            while vals and vals[-1] == "":
                vals.pop()
            out.append(vals)
        while out and not out[-1]:
            out.pop()
        return {"range": rng, "values": out} if out else {"range": rng}

    # -- API --
    def spreadsheets(self):
        return self

    def values(self):
        return _Values(self)

    def get(self, spreadsheetId: str, fields: str | None = None, **_kw):
        return _Req(lambda: {"sheets": [{"properties": {"title": t}} for t in self.data]}, self.calls, "spreadsheets.get")

    def batchUpdate(self, spreadsheetId: str, body: dict):
        def run():
            for req in body.get("requests", []):
                if "addSheet" in req:
                    self.data.setdefault(req["addSheet"]["properties"]["title"], [])
            return {}
        return _Req(run, self.calls, "spreadsheets.batchUpdate")

    # leitura cômoda para relatórios
    def rows_as_dicts(self, sheet: str) -> list[dict[str, str]]:
        grid = self.data.get(sheet) or []
        if not grid:
            return []
        hdr = [h.lower().strip() for h in grid[0]]
        return [{h: (r[i] if i < len(r) else "") for i, h in enumerate(hdr)} for r in grid[1:]]


class _Values:
    def __init__(self, svc: FakeSheetsService):
        self.s = svc

    def get(self, spreadsheetId: str, range: str, **_kw):
        return _Req(lambda: self.s._read(range), self.s.calls, "values.get")

    def batchGet(self, spreadsheetId: str, ranges: list[str], **_kw):
        return _Req(lambda: {"valueRanges": [self.s._read(r) for r in ranges]}, self.s.calls, "values.batchGet")

    def update(self, spreadsheetId: str, range: str, valueInputOption: str, body: dict):
        return _Req(lambda: self.s._write(range, body.get("values", [])) or {}, self.s.calls, "values.update")

    def batchUpdate(self, spreadsheetId: str, body: dict):
        def run():
            for d in body.get("data", []):
                self.s._write(d["range"], d["values"])
            return {}
        return _Req(run, self.s.calls, "values.batchUpdate")

    def append(self, spreadsheetId: str, range: str, valueInputOption: str, insertDataOption: str = "", body: dict | None = None):
        def run():
            sheet, _ = _parse_range(range)
            grid = self.s._grid(sheet)
            for row in (body or {}).get("values", []):
                grid.append([str(v) for v in row])
            return {}
        return _Req(run, self.s.calls, "values.append")


class FakeCalendarService:
    def __init__(self):
        self.created: list[dict[str, Any]] = []
        self.calls: Counter = Counter()

    def events(self):
        return _Events(self)


class _Events:
    def __init__(self, cal: FakeCalendarService):
        self.c = cal

    def insert(self, calendarId: str, body: dict, sendUpdates: str = "", conferenceDataVersion: int = 0):
        def run():
            ev = {**body, "id": uuid.uuid4().hex[:10], "hangoutLink": f"https://meet.google.com/fic-ticio-{len(self.c.created)+1:03d}", "htmlLink": "https://calendar.google.com/FICTICIO"}
            self.c.created.append(ev)
            return ev
        return _Req(run, self.c.calls, "events.insert")

    def list(self, calendarId: str, **kw):
        return _Req(lambda: {"items": list(self.c.created)}, self.c.calls, "events.list")
