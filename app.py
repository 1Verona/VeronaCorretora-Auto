from __future__ import annotations

import re
import threading
import traceback
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import Flask, jsonify, request
from googleapiclient.errors import HttpError

from scraper import (
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_OUTPUT_SHEET,
    DEFAULT_SECCIONAL,
    DEFAULT_SPREADSHEET_ID,
    ScraperConfig,
    allowed_credential_file,
    cleanup_cache,
    inspect_spreadsheet,
    normalize_spreadsheet_id,
    preview_white_leads,
    run_scrape,
)

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = DEFAULT_CREDENTIALS_PATH

PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current_job: dict[str, Any] | None = None
        self._last_job: dict[str, Any] | None = None
        self._stop_events: dict[str, threading.Event] = {}

    def _snapshot(self, job: dict[str, Any] | None) -> dict[str, Any] | None:
        return deepcopy(job) if job else None

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job": self._snapshot(self._current_job),
                "last_job": self._snapshot(self._last_job),
            }

    def append_log(self, job_id: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            job = self._current_job if self._current_job and self._current_job.get("job_id") == job_id else None
            if not job:
                return
            job.setdefault("logs", []).append(f"[{timestamp}] {message}")
            job["message"] = message

            if "CAPTCHA" in message and "resolva no navegador" in message.lower():
                job["captcha_pending"] = True
            elif "captcha resolvido" in message.lower() or "sessão pronta" in message.lower():
                job["captcha_pending"] = False

            m = PROGRESS_RE.search(message)
            if m:
                job["progress"] = {"current": int(m.group(1)), "total": int(m.group(2))}

            if "Tel:" in message and "—" not in message.split("Tel:")[1][:5]:
                job["found"] = job.get("found", 0) + 1

    def start(self, config: ScraperConfig) -> dict[str, Any]:
        with self._lock:
            if self._current_job and self._current_job.get("status") == "running":
                raise RuntimeError("Já existe um processamento em andamento.")

            job_id = uuid4().hex[:12]
            stop_event = threading.Event()
            self._stop_events[job_id] = stop_event

            job = {
                "job_id": job_id,
                "status": "running",
                "message": "Job iniciado.",
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
                "logs": [],
                "captcha_pending": False,
                "progress": {"current": 0, "total": 0},
                "summary": {
                    "spreadsheet_id": config.spreadsheet_id,
                    "output_sheet_name": config.output_sheet_name,
                    "seccional": config.seccional,
                    "limit": config.limit,
                },
            }
            self._current_job = job

        thread = threading.Thread(target=self._worker, args=(job_id, config, stop_event), daemon=True)
        thread.start()
        return self._snapshot(job) or {}

    def stop(self) -> bool:
        with self._lock:
            if not self._current_job or self._current_job.get("status") != "running":
                return False
            job_id = self._current_job["job_id"]
            event = self._stop_events.get(job_id)
            if event:
                event.set()
            return True

    def _worker(self, job_id: str, config: ScraperConfig, stop_event: threading.Event) -> None:
        def logger(message: str) -> None:
            self.append_log(job_id, message)

        def stop_check() -> bool:
            return stop_event.is_set()

        try:
            summary = run_scrape(config, log_callback=logger, stop_check=stop_check)
            with self._lock:
                if not self._current_job or self._current_job.get("job_id") != job_id:
                    return
                stopped = stop_event.is_set()
                self._current_job["status"] = "stopped" if stopped else "completed"
                self._current_job["message"] = "Interrompido pelo usuário." if stopped else summary.get("message", "Concluído.")
                self._current_job["summary"] = summary
                self._current_job["finished_at"] = datetime.now().isoformat(timespec="seconds")
                self._current_job["captcha_pending"] = False
                self._last_job = deepcopy(self._current_job)
                self._current_job = None
                self._stop_events.pop(job_id, None)
        except Exception as exc:
            logger(f"Falha no job: {exc}")
            logger(traceback.format_exc())
            with self._lock:
                failed_job = self._current_job if self._current_job and self._current_job.get("job_id") == job_id else None
                if not failed_job:
                    return
                failed_job["status"] = "failed"
                failed_job["message"] = str(exc)
                failed_job["finished_at"] = datetime.now().isoformat(timespec="seconds")
                failed_job["captcha_pending"] = False
                failed_job.setdefault("summary", {})
                failed_job["summary"]["error"] = str(exc)
                self._last_job = deepcopy(failed_job)
                self._current_job = None
                self._stop_events.pop(job_id, None)


job_manager = JobManager()


def build_config_from_request(form) -> ScraperConfig:
    limit_value = (form.get("limit") or "").strip()
    limit = int(limit_value) if limit_value else None
    return ScraperConfig(
        credentials_path=CREDENTIALS_PATH,
        spreadsheet_id=normalize_spreadsheet_id(form.get("spreadsheet_id")),
        output_sheet_name=(form.get("output_sheet_name") or DEFAULT_OUTPUT_SHEET).strip() or DEFAULT_OUTPUT_SHEET,
        seccional=(form.get("seccional") or DEFAULT_SECCIONAL).strip() or DEFAULT_SECCIONAL,
        limit=limit,
        headless=False,
    )


def inspect_current_connection(spreadsheet_id: str) -> dict[str, Any]:
    payload = inspect_spreadsheet(CREDENTIALS_PATH, spreadsheet_id)
    preview = preview_white_leads(
        ScraperConfig(
            credentials_path=CREDENTIALS_PATH,
            spreadsheet_id=spreadsheet_id,
        )
    )
    payload.update(preview)
    return payload


def format_google_error(exc: HttpError) -> tuple[str, int]:
    status_code = getattr(exc.resp, "status", 500)
    if status_code == 404:
        return "Planilha não encontrada. Confira o Spreadsheet ID.", 400
    if status_code == 403:
        return "Sem acesso à planilha. Compartilhe com o e-mail da Service Account.", 400
    return f"Erro da API Google: {exc}", 500


@app.post("/connect")
def connect_google_sheets():
    uploaded_file = request.files.get("credentials")
    credentials_saved = False

    if uploaded_file and uploaded_file.filename:
        if not allowed_credential_file(uploaded_file.filename):
            return jsonify({"error": "Envie um arquivo JSON válido."}), 400
        uploaded_file.save(CREDENTIALS_PATH)
        credentials_saved = True
    elif not CREDENTIALS_PATH.exists():
        return jsonify({"error": "Envie o credentials.json para continuar."}), 400

    spreadsheet_id = normalize_spreadsheet_id(request.form.get("spreadsheet_id"))

    try:
        payload = inspect_current_connection(spreadsheet_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 400
    except HttpError as exc:
        message, code = format_google_error(exc)
        return jsonify({"error": message}), code
    except Exception as exc:
        return jsonify({"error": f"Falha ao validar: {exc}"}), 500

    payload["credentials_saved"] = credentials_saved
    payload["message"] = "Conexão validada."
    return jsonify(payload)


@app.get("/status")
def credentials_status():
    if not CREDENTIALS_PATH.exists():
        return jsonify({"connected": False, "message": "Nenhum credentials.json encontrado."})

    try:
        payload = inspect_current_connection(DEFAULT_SPREADSHEET_ID)
    except HttpError as exc:
        message, _ = format_google_error(exc)
        return jsonify({"connected": False, "message": message}), 200
    except Exception as exc:
        return jsonify({"connected": False, "message": f"Falha: {exc}"}), 500

    payload["connected"] = True
    payload["message"] = "Credenciais OK."
    return jsonify(payload)


@app.post("/run")
def start_run():
    if not CREDENTIALS_PATH.exists():
        return jsonify({"error": "Configure as credenciais primeiro."}), 400

    try:
        config = build_config_from_request(request.form)
        job = job_manager.start(config)
    except ValueError:
        return jsonify({"error": "Limite precisa ser numérico."}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"error": f"Falha ao iniciar: {exc}"}), 500

    return jsonify({"ok": True, "message": "Job iniciado.", "job": job})


@app.post("/stop")
def stop_run():
    stopped = job_manager.stop()
    if stopped:
        return jsonify({"ok": True, "message": "Parando..."})
    return jsonify({"ok": False, "message": "Nenhum job em execução."}), 404


@app.get("/job")
def job_status():
    state = job_manager.get_state()
    return jsonify(state)


@app.get("/health")
def healthcheck():
    state = job_manager.get_state()
    active = state.get("job")
    return jsonify(
        {
            "ok": True,
            "credentials_present": CREDENTIALS_PATH.exists(),
            "job_running": bool(active and active.get("status") == "running"),
        }
    )


if __name__ == "__main__":
    cleanup_cache()
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
