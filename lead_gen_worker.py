from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import osm_leads
import cnpj_leads
from sheets_config import load_config as load_sheets_config

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "lead_gen_config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "interval_hours": 24,
    "run_osm": True,
    "run_cnpj": True,
    "osm_area": "BR-SC",
    "cnpj_uf": "SC",
    "cnpj_cnae": "6911701",
}

CONFIG_FIELDS = set(DEFAULT_CONFIG.keys())

# Antes da primeira execução periódica, aguarda um pouco para o backend
# terminar de subir (evita concorrência com outros jobs no início).
INITIAL_DELAY_SECONDS = 30


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {**DEFAULT_CONFIG, **{k: v for k, v in data.items() if k in CONFIG_FIELDS}}


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = {**DEFAULT_CONFIG, **{k: v for k, v in config.items() if k in CONFIG_FIELDS}}
    CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


class LeadGenWorker:
    """Roda osm_leads.py e cnpj_leads.py periodicamente em background, gravando
    novos leads diretamente nas abas Leads_OSM_<uf>/Leads_CNPJ_<uf> da planilha
    (dedup automático, só adiciona o que for novo)."""

    def __init__(self, notify_broker: Callable[[str], None] | None = None) -> None:
        self.notify_broker = notify_broker or (lambda _msg: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False
        self._last_run_at: str | None = None
        self._last_result: dict[str, Any] = {}
        self._next_run_at: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        config = load_config()
        with self._lock:
            next_in = None
            if self._next_run_at:
                next_in = max(0, int(self._next_run_at - time.time()))
            return {
                "enabled": bool(config.get("enabled")),
                "interval_hours": config.get("interval_hours"),
                "running": self._running,
                "last_run_at": self._last_run_at,
                "last_result": self._last_result,
                "next_run_in_seconds": next_in,
            }

    def run_now(self) -> dict[str, Any]:
        """Executa imediatamente (síncrono). Usado pelo loop e por /leadgen/run-now."""
        with self._lock:
            if self._running:
                return {"ok": False, "error": "Já existe uma busca de leads em andamento."}
            self._running = True

        config = load_config()
        spreadsheet_id = (load_sheets_config().get("spreadsheet_id") or "").strip()
        result: dict[str, Any] = {}

        if config.get("run_osm", True):
            result["osm"] = self._run_osm(config, spreadsheet_id)
        if config.get("run_cnpj", True):
            result["cnpj"] = self._run_cnpj(config, spreadsheet_id)

        with self._lock:
            self._running = False
            self._last_run_at = datetime.now().isoformat(timespec="seconds")
            self._last_result = result

        return {"ok": True, "result": result}

    def _run_osm(self, config: dict[str, Any], spreadsheet_id: str) -> dict[str, Any]:
        argv = ["--area", str(config.get("osm_area", "BR-SC"))]
        if spreadsheet_id:
            argv += ["--spreadsheet-id", spreadsheet_id]
        try:
            args = osm_leads.parse_args(argv)
            elements = osm_leads.fetch_osm_leads(args.area, log_callback=lambda *_: None)
            rows = osm_leads.build_lead_rows(elements)
            service = osm_leads.build_sheets_service(args.credentials)
            write_result = osm_leads.write_leads_sheet(service, args.spreadsheet_id, args.output_sheet, osm_leads.HEADERS, rows, "ID")
            return {"ok": True, **write_result}
        except Exception as exc:
            traceback.print_exc()
            return {"ok": False, "error": str(exc)}

    def _run_cnpj(self, config: dict[str, Any], spreadsheet_id: str) -> dict[str, Any]:
        argv = ["--uf", str(config.get("cnpj_uf", "SC")), "--cnae", str(config.get("cnpj_cnae", "6911701"))]
        if spreadsheet_id:
            argv += ["--spreadsheet-id", spreadsheet_id]
        try:
            args = cnpj_leads.parse_args(argv)
            client = cnpj_leads.build_bq_client(args.credentials, args.bq_project)
            data_est = cnpj_leads.latest_data_date(client, cnpj_leads.EST_TABLE)
            data_emp = cnpj_leads.latest_data_date(client, cnpj_leads.EMP_TABLE)
            if data_est is None or data_emp is None:
                return {"ok": False, "error": "Snapshot de CNPJ não encontrado."}

            from google.cloud import bigquery

            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("data_est", "DATE", data_est),
                    bigquery.ScalarQueryParameter("data_emp", "DATE", data_emp),
                    bigquery.ScalarQueryParameter("uf", "STRING", args.uf),
                    bigquery.ArrayQueryParameter("situacoes", "STRING", cnpj_leads.SITUACAO_ATIVA),
                    bigquery.ArrayQueryParameter("cnaes", "STRING", sorted(args.cnae)),
                ]
            )
            job = client.query(cnpj_leads.MAIN_QUERY, job_config=job_config)
            result_rows = list(job.result())
            rows = cnpj_leads.build_lead_rows(result_rows, alta_max_anos=args.alta_max_anos, media_max_anos=args.media_max_anos)

            service = cnpj_leads.build_sheets_service(args.credentials)
            write_result = cnpj_leads.write_leads_sheet(service, args.spreadsheet_id, args.output_sheet, cnpj_leads.HEADERS, rows, "CNPJ")
            return {"ok": True, "snapshot": data_est.isoformat(), **write_result}
        except Exception as exc:
            traceback.print_exc()
            return {"ok": False, "error": str(exc)}

    def _loop(self) -> None:
        self._sleep(INITIAL_DELAY_SECONDS)
        while not self._stop.is_set():
            config = load_config()
            if not config.get("enabled"):
                self._sleep(60)
                continue

            try:
                result = self.run_now()
                if result.get("ok"):
                    added = sum(r.get("added", 0) for r in result["result"].values() if isinstance(r, dict))
                    if added:
                        self.notify_broker(f"🔎 Geração automática de leads: {added} novo(s) lead(s) adicionados à planilha.")
            except Exception as exc:
                with self._lock:
                    self._last_result = {"ok": False, "error": str(exc)}
                    self._last_run_at = datetime.now().isoformat(timespec="seconds")

            interval_seconds = max(3600, int(config.get("interval_hours", 24)) * 3600)
            with self._lock:
                self._next_run_at = time.time() + interval_seconds
            self._sleep(interval_seconds)

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set():
            time.sleep(min(1.0, end - time.time()))
