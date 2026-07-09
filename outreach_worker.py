from __future__ import annotations

import json
import os
import random
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pytz
from dotenv import load_dotenv

from agent_config import load_config as load_agent_config
from evolution_client import EvolutionClient, EvolutionError, normalize_phone, to_jid
from sheet_manager import SheetManager
from sheets_config import load_config as load_sheets_config

load_dotenv(Path(__file__).parent / ".env")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "outreach_config.json"
TZ = pytz.timezone(os.getenv("TZ", "America/Sao_Paulo"))

DEFAULT_TEMPLATES = [
    "Olá {nome}, tudo bem? Aqui é da Verona Corretora. Vi seu cadastro e queria te apresentar nossas opções de seguro de vida com condições especiais para advogados. Posso te enviar um resumo rápido?",
    "Oi {nome}! Tudo certo? Sou da Verona Corretora — trabalhamos com seguros pensados pra rotina jurídica. Faz sentido eu te mostrar uma simulação sem compromisso?",
    "Bom dia {nome}, aqui é da Verona Corretora 🙂 Temos planos de seguro vida com vantagens exclusivas pra OAB. Posso te explicar em 2 minutos?",
    "Olá {nome}! Sou consultor da Verona Corretora. Você teria 2 minutos pra ouvir sobre uma cobertura de seguro pensada pra advogados? Sem compromisso.",
    "Oi {nome}, espero que esteja bem. Aqui é da Verona Corretora — ajudamos advogados a economizar em seguros profissionais e de vida. Posso te enviar uma proposta personalizada?",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "hour_start": 9,
    "hour_end": 18,
    "weekdays_only": True,
    "daily_limit": 50,
    "min_delay": 60,
    "max_delay": 180,
    "max_consecutive_errors": 3,
    "templates": DEFAULT_TEMPLATES,
}

CONFIG_FIELDS = set(DEFAULT_CONFIG.keys())


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    merged = {**DEFAULT_CONFIG, **{k: v for k, v in data.items() if k in CONFIG_FIELDS}}
    return merged


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = {**DEFAULT_CONFIG, **{k: v for k, v in config.items() if k in CONFIG_FIELDS}}
    CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def within_window(now: datetime, config: dict[str, Any]) -> bool:
    if config.get("weekdays_only", True) and now.weekday() >= 5:
        return False
    hour = now.hour
    return int(config.get("hour_start", 9)) <= hour < int(config.get("hour_end", 18))


def pick_template(templates: list[str]) -> str:
    pool = [t for t in templates if t and t.strip()]
    if not pool:
        return DEFAULT_TEMPLATES[0]
    return random.choice(pool)


def render_template(template: str, nome: str) -> str:
    """Aplica os placeholders do template de forma segura.
    Usa replace() em vez de format() para não quebrar com placeholders inesperados.
    Garante que nome vazio não gere vírgula solta (ex: 'Olá , tudo bem?').
    """
    first_name = (nome or "").strip().split()[0] if (nome or "").strip() else ""
    display_name = first_name if first_name else "Prezado(a)"
    return template.replace("{nome}", display_name)


class OutreachWorker:
    def __init__(
        self,
        sheet_manager: SheetManager | None = None,
        evolution: EvolutionClient | None = None,
        notify_broker: Callable[[str], None] | None = None,
        conversation_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.sheet_manager = sheet_manager or SheetManager()
        self.evolution = evolution or EvolutionClient()
        self.notify_broker = notify_broker or (lambda _msg: None)
        self.conversation_hook = conversation_hook or (lambda _lead: None)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._consecutive_errors = 0
        self._paused_reason = ""
        self._sent_today = 0
        self._sent_date: date | None = None
        self._next_dispatch_at: float | None = None
        self._last_error: str = ""
        self._queue_cache: tuple[float, int] | None = None  # (timestamp, queue_size)

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
        now = datetime.now(TZ)
        with self._lock:
            self._refresh_daily_counter(now.date())
            queue_size = -1
            try:
                # Cache de 30s para não bater a quota da Google Sheets a cada poll
                cache_ttl = 30.0
                if self._queue_cache and (time.time() - self._queue_cache[0]) < cache_ttl:
                    queue_size = self._queue_cache[1]
                else:
                    source = (load_sheets_config().get("source_sheet") or "").strip() or None
                    agent_cfg = load_agent_config()
                    pool = self.sheet_manager.get_leads_pending_first_contact(source, limit=200)
                    if agent_cfg.get("test_mode"):
                        test_phone = normalize_phone(agent_cfg.get("test_phone") or "")
                        pool = [c for c in pool if normalize_phone(c.get("telefone", "")) == test_phone]
                    queue_size = len(pool)
                    self._queue_cache = (time.time(), queue_size)
            except Exception:
                queue_size = -1
            agent_cfg = load_agent_config()
            next_dispatch = None
            if self._next_dispatch_at:
                remaining = max(0, int(self._next_dispatch_at - time.time()))
                next_dispatch = remaining
            return {
                "enabled": bool(config.get("enabled")),
                "in_window": within_window(now, config),
                "sent_today": self._sent_today,
                "daily_limit": int(config.get("daily_limit", 50)),
                "queue_size": queue_size,
                "next_dispatch_in_seconds": next_dispatch,
                "paused_reason": self._paused_reason,
                "last_error": self._last_error,
                "evolution_configured": self.evolution.configured,
                "test_mode": bool(agent_cfg.get("test_mode")),
                "test_phone": agent_cfg.get("test_phone", ""),
            }

    def _refresh_daily_counter(self, today: date) -> None:
        if self._sent_date != today:
            self._sent_date = today
            self._sent_today = 0

    def dispatch_one_now(self) -> dict[str, Any]:
        config = load_config()
        return self._send_one(config, force=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                config = load_config()
                if not config.get("enabled"):
                    self._paused_reason = "desligado"
                    self._sleep(10)
                    continue

                now = datetime.now(TZ)
                with self._lock:
                    self._refresh_daily_counter(now.date())

                if not within_window(now, config):
                    self._paused_reason = "fora da janela horária"
                    self._sleep(60)
                    continue

                if self._sent_today >= int(config.get("daily_limit", 50)):
                    self._paused_reason = "limite diário atingido"
                    self._sleep(300)
                    continue

                max_errors = int(config.get("max_consecutive_errors", 3))
                if self._consecutive_errors >= max_errors:
                    self._paused_reason = f"pausado após {self._consecutive_errors} erros consecutivos"
                    self._sleep(300)
                    continue

                self._paused_reason = ""
                result = self._send_one(config)
                if result.get("sent"):
                    delay = random.uniform(
                        float(config.get("min_delay", 60)),
                        float(config.get("max_delay", 180)),
                    )
                    with self._lock:
                        self._next_dispatch_at = time.time() + delay
                    self._sleep(delay)
                elif result.get("empty"):
                    self._sleep(30)
                else:
                    self._sleep(15)
            except Exception as exc:
                self._last_error = str(exc)
                self._sleep(15)

    def _send_one(self, config: dict[str, Any], force: bool = False) -> dict[str, Any]:
        if not self.evolution.configured:
            self._paused_reason = "Evolution não configurado"
            return {"sent": False, "error": "evolution_not_configured"}

        source = (load_sheets_config().get("source_sheet") or "").strip() or None
        agent_cfg = load_agent_config()
        if agent_cfg.get("test_mode"):
            test_phone = normalize_phone(agent_cfg.get("test_phone") or "")
            if not test_phone:
                self._paused_reason = "modo de teste sem número configurado"
                return {"sent": False, "error": "test_phone_missing"}
            pool = self.sheet_manager.get_leads_pending_first_contact(source, limit=100)
            candidates = [c for c in pool if normalize_phone(c.get("telefone", "")) == test_phone]
        else:
            candidates = self.sheet_manager.get_leads_pending_first_contact(source, limit=1)

        if not candidates:
            return {"sent": False, "empty": True}

        lead = candidates[0]
        template = pick_template(config.get("templates") or DEFAULT_TEMPLATES)
        text = render_template(template, lead.get("nome", ""))

        if not text.strip():
            return {"sent": False, "error": "template resultou em mensagem vazia"}

        try:
            self.evolution.send_text(lead["telefone"], text)
        except EvolutionError as exc:
            self._consecutive_errors += 1
            self._last_error = str(exc)
            max_errors = int(config.get("max_consecutive_errors", 3))
            if self._consecutive_errors >= max_errors:
                self.notify_broker(
                    f"⚠️ Disparo pausado: {self._consecutive_errors} erros consecutivos.\nÚltimo erro: {exc}"
                )
            return {"sent": False, "error": str(exc), "lead": lead}

        self._consecutive_errors = 0
        now = datetime.now(TZ)
        try:
            self.sheet_manager.mark_first_contact(
                sheet_name=lead["sheet"],
                row=lead["row"],
                when=now,
                template_used=template,
                jid=to_jid(lead["telefone"]),
            )
        except Exception as exc:
            self._last_error = f"falha ao atualizar planilha: {exc}"

        with self._lock:
            self._sent_today += 1
            if self._sent_today >= int(config.get("daily_limit", 50)):
                self.notify_broker(f"📊 Limite diário atingido ({self._sent_today} envios).")

        try:
            self.conversation_hook({**lead, "template_used": template, "first_contact_at": now.isoformat()})
        except Exception:
            pass

        return {"sent": True, "lead": lead, "template": template}

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set():
            time.sleep(min(1.0, end - time.time()))
