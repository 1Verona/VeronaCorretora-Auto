from __future__ import annotations

import hashlib
import hmac as _hmac
import os
import re
import socket
import threading
import traceback
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import Flask, jsonify, request, send_from_directory
from googleapiclient.errors import HttpError

from agent_config import load_config as load_agent_cfg, save_config as save_agent_cfg, tools_catalog
from lead_gen_worker import LeadGenWorker
from lead_gen_worker import load_config as load_leadgen_config, save_config as save_leadgen_config
from outreach_worker import OutreachWorker, load_config, save_config
from scraper import (
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_OUTPUT_SHEET,
    DEFAULT_SECCIONAL,
    DEFAULT_SPREADSHEET_ID,
    ScraperConfig,
    allowed_credential_file,
    apply_priority,
    build_sheets_service,
    cleanup_cache,
    count_leads_per_sheet,
    inspect_spreadsheet,
    load_priority_config,
    normalize_spreadsheet_id,
    preview_white_leads,
    run_scrape,
    save_priority_config,
)
from sheets_config import load_config as load_sheets_cfg, save_config as save_sheets_cfg
from telegram_bot import TELEGRAM_BOT_TOKEN, TelegramBot

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = DEFAULT_CREDENTIALS_PATH
FLASK_PORT_FILE = BASE_DIR / ".flask_port"
ENV_PATH = BASE_DIR / ".env"

# Restaurar credentials.json a partir da env se não existir localmente (útil em ambientes como Easypanel)
google_creds_env = os.environ.get("GOOGLE_CREDENTIALS")
if google_creds_env and not CREDENTIALS_PATH.exists():
    try:
        import json
        json_data = json.loads(google_creds_env)
        CREDENTIALS_PATH.write_text(json.dumps(json_data, indent=2), encoding="utf-8")
        print(" * credentials.json gerado a partir da variável de ambiente GOOGLE_CREDENTIALS")
    except Exception as e:
        print(f" * Erro ao decodificar GOOGLE_CREDENTIALS da env: {e}")


def _read_env_file() -> dict[str, str]:
    """Lê o .env como dict preservando todas as linhas."""
    if not ENV_PATH.exists():
        return {}
    result: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        result[key.strip()] = val.strip()
    return result


def _write_env_file(values: dict[str, str]) -> None:
    """Reescreve o .env atualizando as chaves fornecidas (preserva ordem e comentários)."""
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.partition("=")[0].strip()
        if key in values:
            new_lines.append(f"{key}={values[key]}")
            updated_keys.add(key)
        else:
            new_lines.append(line)

    # Adiciona chaves novas que não existiam no arquivo
    for key, val in values.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _reload_evolution_client() -> None:
    """Recria o EvolutionClient com os valores atuais do os.environ."""
    from evolution_client import EvolutionClient
    outreach_worker.evolution = EvolutionClient()


def find_free_port(start: int = 5050, max_attempts: int = 10) -> int:
    for port in range(start, start + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"Nenhuma porta livre entre {start} e {start + max_attempts - 1}.")

PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]")

app = Flask(
    __name__,
    static_folder=os.path.join(os.path.dirname(__file__), "frontend", "dist"),
    static_url_path="/"
)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_ADMIN_USER = os.getenv("ADMIN_USER", "Admin").strip()
_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
_APP_SECRET = os.getenv("APP_SECRET", "verona-dev-secret").strip()

_AUTH_SKIP_PREFIXES = ("/webhook/",)
_AUTH_SKIP_EXACT = {"/login"}


def _make_token(user: str, pw: str) -> str:
    msg = f"{user}:{pw}".encode()
    return _hmac.new(_APP_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def _verify_token(token: str) -> bool:
    if not _ADMIN_PASSWORD:
        return True  # sem senha configurada = acesso livre (dev local)
    expected = _make_token(_ADMIN_USER, _ADMIN_PASSWORD)
    return _hmac.compare_digest(token, expected)


@app.before_request
def check_auth():
    if request.method == "OPTIONS":
        return None
    path = request.path
    if path in _AUTH_SKIP_EXACT or any(path.startswith(p) for p in _AUTH_SKIP_PREFIXES):
        return None
    if not _ADMIN_PASSWORD:
        return None  # auth desativada localmente
    auth = request.headers.get("Authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    if not token or not _verify_token(token):
        return jsonify({"error": "Não autorizado", "auth_required": True}), 401
    return None


@app.post("/login")
def login():
    payload = request.get_json(silent=True) or {}
    user = (payload.get("username") or "").strip()
    pw = (payload.get("password") or "").strip()
    if not _ADMIN_PASSWORD:
        return jsonify({"ok": True, "token": "no-auth"})
    if user == _ADMIN_USER and pw == _ADMIN_PASSWORD:
        return jsonify({"ok": True, "token": _make_token(user, pw)})
    return jsonify({"ok": False, "error": "Usuário ou senha incorretos"}), 401


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
telegram_bot = TelegramBot(
    token=TELEGRAM_BOT_TOKEN,
    flask_url="http://127.0.0.1:5050",
)


def _notify_broker(text: str) -> None:
    try:
        telegram_bot.send_broker_notification(text)
    except Exception:
        pass


def _conversation_seed(lead: dict[str, Any]) -> None:
    try:
        from conversation_engine import register_outbound_first_contact

        register_outbound_first_contact(lead)
    except Exception:
        pass


outreach_worker = OutreachWorker(
    notify_broker=_notify_broker,
    conversation_hook=_conversation_seed,
)
lead_gen_worker = LeadGenWorker(notify_broker=_notify_broker)


def build_config_from_request(form) -> ScraperConfig:
    limit_value = (form.get("limit") or "").strip()
    limit = int(limit_value) if limit_value else None
    cfg = load_sheets_cfg()
    return ScraperConfig(
        credentials_path=CREDENTIALS_PATH,
        spreadsheet_id=normalize_spreadsheet_id(form.get("spreadsheet_id") or cfg["spreadsheet_id"]),
        output_sheet_name=(form.get("output_sheet_name") or cfg["output_sheet_name"]).strip() or cfg["output_sheet_name"],
        seccional=(form.get("seccional") or cfg["seccional"]).strip() or cfg["seccional"],
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
        payload = inspect_current_connection(load_sheets_cfg()["spreadsheet_id"])
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


@app.get("/sources")
def list_sources():
    if not CREDENTIALS_PATH.exists():
        return jsonify({"error": "Configure as credenciais primeiro."}), 400
    try:
        service = build_sheets_service(CREDENTIALS_PATH)
        counts = count_leads_per_sheet(service, load_sheets_cfg()["spreadsheet_id"])
    except HttpError as exc:
        message, code = format_google_error(exc)
        return jsonify({"error": message}), code
    except Exception as exc:
        return jsonify({"error": f"Falha ao listar abas: {exc}"}), 500

    cfg = load_priority_config()
    available = list(counts.keys())
    order = cfg.get("order", [])
    enabled = cfg.get("enabled", {})

    ordered = [s for s in order if s in counts]
    extras = [s for s in available if s not in ordered]
    final_order = ordered + extras

    sources = [
        {
            "name": title,
            "lead_count": counts.get(title, 0),
            "enabled": enabled.get(title, True),
        }
        for title in final_order
    ]
    return jsonify({"sources": sources})


@app.post("/sources")
def update_sources():
    payload = request.get_json(silent=True) or {}
    sources = payload.get("sources")
    if not isinstance(sources, list):
        return jsonify({"error": "Formato inválido: esperado { sources: [...] }"}), 400

    order: list[str] = []
    enabled: dict[str, bool] = {}
    for item in sources:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        order.append(name)
        enabled[name] = bool(item.get("enabled", True))

    config = save_priority_config(order, enabled)
    return jsonify({"ok": True, "config": config})


@app.get("/leadgen/status")
def leadgen_status():
    return jsonify(lead_gen_worker.status())


@app.post("/leadgen/config")
def leadgen_set_config():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Formato inválido"}), 400
    try:
        merged = {**load_leadgen_config(), **payload}
        if "interval_hours" in merged:
            merged["interval_hours"] = int(merged["interval_hours"])
        saved = save_leadgen_config(merged)
        return jsonify({"ok": True, "config": saved})
    except (TypeError, ValueError) as exc:
        return jsonify({"error": f"Valor inválido: {exc}"}), 400


@app.post("/leadgen/run-now")
def leadgen_run_now():
    def _runner():
        lead_gen_worker.run_now()

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"ok": True, "message": "Busca de novos leads iniciada em background."})


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


@app.get("/outreach/config")
def outreach_get_config():
    return jsonify(load_config())


@app.post("/outreach/config")
def outreach_set_config():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Formato inválido"}), 400
    try:
        current = load_config()
        merged = {**current, **payload}
        if "templates" in merged and not isinstance(merged["templates"], list):
            return jsonify({"error": "templates precisa ser lista"}), 400
        for int_field in ("hour_start", "hour_end", "daily_limit", "min_delay", "max_delay", "max_consecutive_errors"):
            if int_field in merged:
                merged[int_field] = int(merged[int_field])
        saved = save_config(merged)
        return jsonify({"ok": True, "config": saved})
    except (TypeError, ValueError) as exc:
        return jsonify({"error": f"Valor inválido: {exc}"}), 400


@app.post("/outreach/start")
def outreach_start():
    config = load_config()
    config["enabled"] = True
    save_config(config)
    outreach_worker.start()
    return jsonify({"ok": True, "config": config})


@app.post("/outreach/stop")
def outreach_stop():
    config = load_config()
    config["enabled"] = False
    save_config(config)
    return jsonify({"ok": True, "config": config})


@app.get("/outreach/status")
def outreach_status():
    return jsonify(outreach_worker.status())


@app.post("/outreach/dispatch-now")
def outreach_dispatch_now():
    try:
        result = outreach_worker.dispatch_one_now()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    code = 200 if result.get("sent") else 409
    return jsonify(result), code


@app.get("/outreach/conversations")
def outreach_list_conversations():
    try:
        from conversation_engine import list_conversations

        return jsonify({"conversations": list_conversations()})
    except Exception as exc:
        return jsonify({"error": str(exc), "conversations": []}), 500


@app.post("/outreach/conversations/<phone>/pause")
def outreach_pause_conversation(phone: str):
    try:
        from conversation_engine import set_paused

        ok = set_paused(phone, True)
        return jsonify({"ok": ok})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/outreach/conversations/<phone>/resume")
def outreach_resume_conversation(phone: str):
    try:
        from conversation_engine import set_paused

        ok = set_paused(phone, False)
        return jsonify({"ok": ok})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/agent/config")
def agent_get_config():
    return jsonify(load_agent_cfg())


@app.post("/agent/config")
def agent_set_config():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Formato inválido"}), 400
    try:
        saved = save_agent_cfg(payload)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": f"Valor inválido: {exc}"}), 400
    return jsonify({"ok": True, "config": saved})


@app.get("/agent/tools")
def agent_get_tools():
    return jsonify({"tools": tools_catalog()})


@app.get("/sheets/config")
def sheets_get_config():
    return jsonify(load_sheets_cfg())


@app.post("/sheets/config")
def sheets_set_config():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Formato inválido"}), 400
    if "spreadsheet_id" in payload:
        payload["spreadsheet_id"] = normalize_spreadsheet_id(payload.get("spreadsheet_id"))
    saved = save_sheets_cfg(payload)
    return jsonify({"ok": True, "config": saved})


@app.get("/evolution/config")
def evolution_get_config():
    """Retorna as configura\u00e7\u00f5es atuais da Evolution (mascarando a API key)."""
    env = _read_env_file()
    api_key = env.get("EVOLUTION_API_KEY", "")
    masked_key = (api_key[:6] + "***") if len(api_key) > 6 else ("***" if api_key else "")
    from evolution_client import EvolutionClient
    client = EvolutionClient()
    return jsonify({
        "evolution_api_url": env.get("EVOLUTION_API_URL", ""),
        "evolution_api_key_set": bool(api_key),
        "evolution_api_key_masked": masked_key,
        "evolution_instance": env.get("EVOLUTION_INSTANCE", ""),
        "evolution_webhook_token": env.get("EVOLUTION_WEBHOOK_TOKEN", ""),
        "configured": client.configured,
    })


@app.post("/evolution/config")
def evolution_set_config():
    """Salva as credenciais da Evolution no .env e recarrega o cliente."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Formato inv\u00e1lido"}), 400

    allowed = {"evolution_api_url", "evolution_api_key", "evolution_instance", "evolution_webhook_token"}
    updates: dict[str, str] = {}

    if "evolution_api_url" in payload:
        url = str(payload["evolution_api_url"]).strip().rstrip("/")
        updates["EVOLUTION_API_URL"] = url
    if "evolution_api_key" in payload:
        key = str(payload["evolution_api_key"]).strip()
        if key and not key.endswith("***"):
            updates["EVOLUTION_API_KEY"] = key
    if "evolution_instance" in payload:
        updates["EVOLUTION_INSTANCE"] = str(payload["evolution_instance"]).strip()
    if "evolution_webhook_token" in payload:
        updates["EVOLUTION_WEBHOOK_TOKEN"] = str(payload["evolution_webhook_token"]).strip()

    if not updates:
        return jsonify({"error": "Nenhum campo v\u00e1lido enviado"}), 400

    # Salva no .env
    _write_env_file(updates)
    # Aplica no os.environ para o processo atual
    for k, v in updates.items():
        os.environ[k] = v

    # Recarrega o EvolutionClient com as novas credenciais
    try:
        _reload_evolution_client()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Salvo no .env mas falha ao recarregar cliente: {exc}"}), 500

    return jsonify({"ok": True, "message": "Credenciais salvas e aplicadas."})


@app.get("/evolution/qr")
def evolution_qr():
    """Retorna QR code da inst\u00e2ncia Evolution para o cliente escanear no frontend."""
    from evolution_client import EvolutionClient
    client = EvolutionClient()
    if not client.configured:
        return jsonify({"connected": False, "qr_base64": None, "error": "Evolution n\u00e3o configurado. Preencha URL, API Key e Instance primeiro."})
    try:
        result = client.get_qr_code()
        return jsonify(result)
    except Exception as exc:
        return jsonify({"connected": False, "qr_base64": None, "error": str(exc)}), 500


@app.post("/webhook/evolution")
@app.post("/webhook/evolution/<event>")
def evolution_webhook(event=None):
    expected_token = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "").strip()
    if expected_token:
        auth = request.headers.get("Authorization", "")
        token_query = request.args.get("token", "")
        provided = ""
        if auth.lower().startswith("bearer "):
            provided = auth.split(" ", 1)[1].strip()
        elif token_query:
            provided = token_query.strip()
        if provided != expected_token:
            return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    import json as _json
    print(f"[WEBHOOK DEBUG] event={event}")
    print(f"[WEBHOOK DEBUG] payload keys: {list(payload.keys())}")
    print(f"[WEBHOOK DEBUG] payload (truncated): {_json.dumps(payload, ensure_ascii=False)[:1000]}")
    try:
        from conversation_engine import handle_inbound_payload

        result = handle_inbound_payload(payload, notify_broker=_notify_broker)
        print(f"[WEBHOOK DEBUG] result: {result}")
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if path != "" and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    else:
        return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    cleanup_cache()
    port = find_free_port(5050)
    FLASK_PORT_FILE.write_text(str(port))
    telegram_bot.flask_url = f"http://127.0.0.1:{port}"
    print(f" * Backend na porta {port}")
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    flask_thread.start()

    outreach_worker.start()
    lead_gen_worker.start()

    if not TELEGRAM_BOT_TOKEN:
        print(" * AVISO: TELEGRAM_BOT_TOKEN não definido no .env — bot do Telegram desligado.")
        flask_thread.join()
    else:
        telegram_bot.start()
