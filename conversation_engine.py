from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pytz
from dotenv import load_dotenv

from agent_config import active_tools, load_config as load_agent_config
from evolution_client import EvolutionClient, EvolutionError, jid_to_phone, normalize_phone
from sheet_manager import SheetManager

load_dotenv(Path(__file__).parent / ".env")

BASE_DIR = Path(__file__).resolve().parent
CONVERSATIONS_PATH = BASE_DIR / "conversations.json"
TZ = pytz.timezone(os.getenv("TZ", "America/Sao_Paulo"))

STAGES = {
    "NEW",
    "ENGAGED",
    "COLLECTING_NAME",
    "COLLECTING_DATETIME",
    "COLLECTING_EMAIL",
    "AWAITING_CONFIRMATION",
    "SCHEDULED",
    "NOT_INTERESTED",
    "HUMAN_TAKEOVER",
}

_lock = threading.Lock()
_evolution = EvolutionClient()
_sheets = SheetManager()
_openai_client = None


def _client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI

        _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "").strip())
    return _openai_client


def _load_all() -> dict[str, Any]:
    if not CONVERSATIONS_PATH.exists():
        return {}
    try:
        return json.loads(CONVERSATIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_all(data: dict[str, Any]) -> None:
    CONVERSATIONS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get(phone: str) -> dict[str, Any] | None:
    data = _load_all()
    return data.get(phone)


def _upsert(phone: str, conv: dict[str, Any]) -> None:
    with _lock:
        data = _load_all()
        data[phone] = conv
        _save_all(data)


def _history_limit() -> int:
    try:
        return int(load_agent_config().get("history_limit", 20))
    except Exception:
        return 20


def _append_history(conv: dict[str, Any], role: str, content: str) -> None:
    conv.setdefault("history", []).append({"role": role, "content": content, "ts": time.time()})
    limit = _history_limit()
    if len(conv["history"]) > limit:
        conv["history"] = conv["history"][-limit:]


def register_outbound_first_contact(lead: dict[str, Any]) -> None:
    phone = normalize_phone(lead.get("telefone", ""))
    if not phone:
        return
    conv = {
        "phone": phone,
        "nome": lead.get("nome", ""),
        "sheet": lead.get("sheet", ""),
        "row": lead.get("row"),
        "stage": "ENGAGED",
        "collected": {},
        "history": [],
        "paused_by_broker": False,
        "first_contact_at": lead.get("first_contact_at") or datetime.now(TZ).isoformat(),
        "last_inbound_ts": None,
        "last_outbound_ts": time.time(),
    }
    if lead.get("template_used"):
        _append_history(conv, "assistant", lead["template_used"])
    _upsert(phone, conv)


def list_conversations() -> list[dict[str, Any]]:
    data = _load_all()
    out = []
    for phone, conv in data.items():
        last_msg = ""
        history = conv.get("history") or []
        if history:
            last_msg = history[-1].get("content", "")[:120]
        out.append({
            "phone": phone,
            "nome": conv.get("nome", ""),
            "sheet": conv.get("sheet", ""),
            "row": conv.get("row"),
            "stage": conv.get("stage", ""),
            "paused_by_broker": conv.get("paused_by_broker", False),
            "first_contact_at": conv.get("first_contact_at", ""),
            "last_inbound_ts": conv.get("last_inbound_ts"),
            "last_outbound_ts": conv.get("last_outbound_ts"),
            "last_message_preview": last_msg,
        })
    out.sort(key=lambda x: (x.get("last_inbound_ts") or 0), reverse=True)
    return out


def set_paused(phone: str, paused: bool) -> bool:
    normalized = normalize_phone(phone)
    with _lock:
        data = _load_all()
        if normalized not in data:
            return False
        data[normalized]["paused_by_broker"] = paused
        if paused:
            data[normalized]["stage"] = "HUMAN_TAKEOVER"
        _save_all(data)
    return True


def _extract_message_from_payload(payload: dict[str, Any]) -> tuple[str, str]:
    data = payload.get("data") or payload
    key = data.get("key") or {}
    if key.get("fromMe"):
        return "", ""
    remote_jid = key.get("remoteJid") or data.get("remoteJid") or ""
    phone = jid_to_phone(remote_jid) if remote_jid else ""
    if not phone:
        phone = data.get("from") or payload.get("from") or ""
    phone = normalize_phone(phone)

    message_obj = data.get("message") or {}
    text = (
        message_obj.get("conversation")
        or (message_obj.get("extendedTextMessage") or {}).get("text")
        or data.get("text")
        or payload.get("text")
        or ""
    )
    if isinstance(text, dict):
        text = text.get("text", "")
    return phone, (text or "").strip()


def handle_inbound_payload(
    payload: dict[str, Any],
    notify_broker: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    notify_broker = notify_broker or (lambda _msg: None)
    phone, text = _extract_message_from_payload(payload)
    if not phone or not text:
        return {"ignored": True, "reason": "sem phone/text"}

    conv = _get(phone)
    if not conv:
        lead = _sheets.find_lead_by_phone(phone)
        conv = {
            "phone": phone,
            "nome": (lead or {}).get("nome", ""),
            "sheet": (lead or {}).get("sheet", ""),
            "row": (lead or {}).get("row"),
            "stage": "ENGAGED" if lead else "NEW",
            "collected": {},
            "history": [],
            "paused_by_broker": False,
            "first_contact_at": None,
            "last_inbound_ts": time.time(),
            "last_outbound_ts": None,
        }

    if conv.get("paused_by_broker"):
        _append_history(conv, "user", text)
        conv["last_inbound_ts"] = time.time()
        _upsert(phone, conv)
        notify_broker(f"💬 Lead {conv.get('nome') or phone} respondeu (conversa pausada para humano):\n\n{text}")
        return {"paused": True}

    _append_history(conv, "user", text)
    conv["last_inbound_ts"] = time.time()

    decision = _llm_decide(conv, text)
    result = _apply_decision(conv, decision, notify_broker)
    _upsert(phone, conv)
    return result


def _llm_decide(conv: dict[str, Any], inbound_text: str) -> dict[str, Any]:
    client = _client()
    cfg = load_agent_config()
    history_limit = int(cfg.get("history_limit", 20))
    messages = [
        {
            "role": "system",
            "content": (
                f"{cfg.get('system_prompt', '')}\n\n"
                f"Estágio atual: {conv.get('stage', 'ENGAGED')}\n"
                f"Dados já coletados: {json.dumps(conv.get('collected', {}), ensure_ascii=False)}\n"
                f"Nome conhecido na planilha: {conv.get('nome', '(desconhecido)')}\n"
                f"Hora atual: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M %Z')}"
            ),
        }
    ]
    for entry in (conv.get("history") or [])[-history_limit:]:
        role = entry.get("role", "user")
        if role not in ("user", "assistant"):
            role = "user"
        messages.append({"role": role, "content": entry.get("content", "")})

    response = client.chat.completions.create(
        model=cfg.get("model", "gpt-4o-mini"),
        messages=messages,
        tools=active_tools(),
        tool_choice="auto",
        temperature=float(cfg.get("temperature", 0.4)),
    )
    choice = response.choices[0].message
    tool_calls = choice.tool_calls or []
    if tool_calls:
        call = tool_calls[0]
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        return {"tool": call.function.name, "args": args}
    return {"tool": "responder", "args": {"mensagem": choice.content or "Pode me contar um pouco mais?", "novo_estagio": "ENGAGED"}}


def _send_outbound(conv: dict[str, Any], text: str) -> None:
    try:
        _evolution.send_text(conv["phone"], text)
        _append_history(conv, "assistant", text)
        conv["last_outbound_ts"] = time.time()
    except EvolutionError as exc:
        conv["last_error"] = str(exc)


def _apply_decision(
    conv: dict[str, Any],
    decision: dict[str, Any],
    notify_broker: Callable[[str], None],
) -> dict[str, Any]:
    tool = decision.get("tool")
    args = decision.get("args") or {}

    if tool == "responder":
        msg = args.get("mensagem", "").strip() or "Pode me contar mais?"
        new_stage = args.get("novo_estagio") or conv.get("stage", "ENGAGED")
        _send_outbound(conv, msg)
        conv["stage"] = new_stage
        _sync_stage(conv)
        return {"action": "responder", "stage": new_stage}

    if tool == "coletar_dado":
        campo = args.get("campo", "")
        valor = (args.get("valor") or "").strip()
        if campo and valor:
            conv.setdefault("collected", {})[campo] = valor
        msg = args.get("mensagem", "").strip()
        new_stage = args.get("novo_estagio") or conv.get("stage")
        if msg:
            _send_outbound(conv, msg)
        conv["stage"] = new_stage
        _sync_stage(conv)
        return {"action": "coletar", "campo": campo, "valor": valor, "stage": new_stage}

    if tool == "agendar":
        return _do_schedule(conv, args, notify_broker)

    if tool == "marcar_sem_interesse":
        motivo = args.get("motivo", "")
        despedida = args.get("mensagem_despedida", "").strip()
        if despedida:
            _send_outbound(conv, despedida)
        conv["stage"] = "NOT_INTERESTED"
        _close_not_interested(conv, motivo)
        notify_broker(f"❌ Lead {conv.get('nome') or conv['phone']} sem interesse.\nMotivo: {motivo}")
        return {"action": "sem_interesse", "motivo": motivo}

    if tool == "pedir_humano":
        motivo = args.get("motivo", "")
        msg = args.get("mensagem_lead", "").strip() or "Vou pedir para um consultor te chamar daqui a pouco, ok?"
        _send_outbound(conv, msg)
        conv["stage"] = "HUMAN_TAKEOVER"
        conv["paused_by_broker"] = True
        notify_broker(f"🙋 Lead {conv.get('nome') or conv['phone']} pediu humano.\nMotivo: {motivo}\nÚltima msg: {(conv.get('history') or [{}])[-2].get('content','')}")
        return {"action": "human_takeover", "motivo": motivo}

    return {"action": "noop"}


def _do_schedule(
    conv: dict[str, Any],
    args: dict[str, Any],
    notify_broker: Callable[[str], None],
) -> dict[str, Any]:
    nome_completo = args.get("nome_completo", "").strip() or conv.get("nome", "")
    data_hora_iso = args.get("data_hora_iso", "").strip()
    email = args.get("email", "").strip()
    confirm = args.get("mensagem_confirmacao", "").strip()

    if not (nome_completo and data_hora_iso and email):
        _send_outbound(conv, "Pra confirmar, preciso de nome completo, data/horário e seu email. Pode me passar de novo?")
        return {"action": "agendar", "ok": False, "reason": "missing_fields"}

    try:
        when = datetime.fromisoformat(data_hora_iso)
        if when.tzinfo is None:
            when = TZ.localize(when)
    except ValueError:
        _send_outbound(conv, "Não consegui entender a data/horário. Pode escrever no formato dia/mês/ano e hora? Ex: 15/05/2026 às 14h.")
        return {"action": "agendar", "ok": False, "reason": "bad_datetime"}

    try:
        from calendar_manager import CalendarManager

        cal = CalendarManager()
        event = cal.create_event(
            start=when,
            lead_name=nome_completo,
            lead_email=email,
            summary=f"Verona Corretora — Reunião com {nome_completo}",
            description=f"Reunião com lead originado via WhatsApp ({conv['phone']}).",
        )
        meet_link = event.get("hangoutLink") or event.get("htmlLink") or ""
    except Exception as exc:
        _send_outbound(conv, "Tive um problema técnico ao criar o convite. Vou pedir pro consultor humano confirmar com você, tudo bem?")
        notify_broker(f"⚠️ Falha ao criar evento para {nome_completo} ({conv['phone']}): {exc}")
        conv["stage"] = "HUMAN_TAKEOVER"
        conv["paused_by_broker"] = True
        return {"action": "agendar", "ok": False, "reason": str(exc)}

    conv.setdefault("collected", {})["nome_completo"] = nome_completo
    conv["collected"]["data_hora"] = when.isoformat()
    conv["collected"]["email"] = email
    conv["stage"] = "SCHEDULED"
    conv["meet_link"] = meet_link

    if not confirm:
        confirm = f"Reunião confirmada para {when.strftime('%d/%m/%Y às %H:%M')}. Vou te enviar o convite por e-mail."
    if meet_link:
        confirm = f"{confirm}\nLink: {meet_link}"
    _send_outbound(conv, confirm)

    sheet = conv.get("sheet")
    row = conv.get("row")
    if sheet and row:
        try:
            _sheets.mark_meeting_scheduled(
                sheet_name=sheet,
                row=row,
                when=when,
                meet_link=meet_link,
                notes=f"Agendado via bot WhatsApp. Email: {email}",
            )
        except Exception as exc:
            notify_broker(f"⚠️ Reunião criada mas falhou ao atualizar planilha: {exc}")

    notify_broker(
        f"✅ Reunião agendada!\n*{nome_completo}*\n📅 {when.strftime('%d/%m/%Y %H:%M')}\n📧 {email}\n📞 {conv['phone']}\n{meet_link or ''}"
    )
    return {"action": "agendar", "ok": True, "datetime": when.isoformat(), "meet_link": meet_link}


def _sync_stage(conv: dict[str, Any]) -> None:
    sheet = conv.get("sheet")
    row = conv.get("row")
    stage = conv.get("stage")
    if sheet and row and stage:
        try:
            _sheets.update_stage(sheet, row, stage)
        except Exception:
            pass


def _close_not_interested(conv: dict[str, Any], motivo: str) -> None:
    sheet = conv.get("sheet")
    row = conv.get("row")
    if sheet and row:
        try:
            _sheets.mark_not_interested(sheet, row, motivo)
        except Exception:
            pass
