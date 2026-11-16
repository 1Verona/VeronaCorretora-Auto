from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pytz
from dotenv import load_dotenv

from agent_config import active_tools, load_config as load_agent_config
from evolution_client import EvolutionClient, EvolutionError, jid_to_phone, normalize_phone, phone_variants
from sheet_manager import SheetManager
import optout
import storage
from log_setup import get_logger
from security import mask_phone

log = get_logger("conversation")

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

_lock = threading.RLock()  # reentrante: _upsert é chamado dentro de blocos que já seguram o lock
# Um lock por telefone: mensagens do MESMO lead são processadas em ordem (sem
# perder histórico), e leads diferentes não esperam uns pelos outros (antes um
# lock global segurava todos durante a busca na planilha).
_phone_locks: dict[str, threading.Lock] = {}
_phone_locks_guard = threading.Lock()


def _phone_lock(phone: str) -> threading.Lock:
    key = min(phone_variants(phone) or {phone})
    with _phone_locks_guard:
        lk = _phone_locks.get(key)
        if lk is None:  # ~100 bytes por lead; não podamos para não quebrar a exclusão
            lk = _phone_locks[key] = threading.Lock()
        return lk
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
    return storage.all_conversations()


def _get(phone: str) -> dict[str, Any] | None:
    return storage.get_conversation(phone)


def _upsert(phone: str, conv: dict[str, Any]) -> None:
    with _lock:
        storage.save_conversation(phone, conv)


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
        conv = storage.get_conversation(normalized)
        if conv is None:
            return False
        conv["paused_by_broker"] = paused
        if paused:
            conv["stage"] = "HUMAN_TAKEOVER"
        storage.save_conversation(normalized, conv)
    return True


def _message_id(payload: dict[str, Any]) -> str:
    data = payload.get("data") or payload
    if not isinstance(data, dict):
        return ""
    key = data.get("key") or {}
    return str(key.get("id") or data.get("id") or "").strip()


def _extract_message_from_payload(payload: dict[str, Any]) -> tuple[str, str]:
    data = payload.get("data") or payload
    key = data.get("key") or {}
    if key.get("fromMe"):
        return "", ""
    remote_jid = key.get("remoteJid") or data.get("remoteJid") or ""
    # Ignorar mensagens de grupos (JID termina em @g.us)
    if remote_jid.endswith("@g.us"):
        return "", ""
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
    data = payload.get("data")
    if data is not None and not isinstance(data, dict):
        return {"ignored": True, "reason": "payload inválido"}
    phone, text = _extract_message_from_payload(payload)
    if not phone or not text:
        return {"ignored": True, "reason": "sem phone/text"}

    agent_cfg = load_agent_config()
    if agent_cfg.get("test_mode"):
        test_phone = normalize_phone(agent_cfg.get("test_phone") or "")
        if not test_phone or not (phone_variants(phone) & phone_variants(test_phone)):
            return {"ignored": True, "reason": "test_mode", "test_phone": test_phone}

    # Evolution reenvia o webhook se demorarmos a responder: o mesmo id não é processado 2x
    msg_id = _message_id(payload)
    if msg_id and not storage.mark_message_seen(msg_id):
        return {"ignored": True, "reason": "duplicado"}

    with _phone_lock(phone):
        if optout.is_opt_out(text):
            return _handle_opt_out(phone, text, notify_broker)

        if storage.is_blocked(phone):
            # já pediu para sair: não responder nunca mais
            return {"ignored": True, "reason": "blocked"}

        conv = _get(phone)
        if not conv:
            try:
                lead = _sheets.find_lead_by_phone(phone)
            except Exception:
                lead = None
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

        _append_history(conv, "user", text)
        conv["last_inbound_ts"] = time.time()

        if conv.get("paused_by_broker"):
            _upsert(phone, conv)
            notify_broker(f"💬 Lead {conv.get('nome') or mask_phone(phone)} respondeu (conversa pausada para humano):\n\n{text}")
            return {"paused": True}

        try:
            decision = _llm_decide(conv, text)
        except Exception as exc:
            # IA fora do ar / timeout: não manda nada errado ao lead, guarda a msg e avisa o corretor
            _upsert(phone, conv)
            log.warning("falha na IA ao responder lead", extra={"ctx": {"phone": mask_phone(phone), "error": str(exc)[:200]}})
            notify_broker(f"⚠️ IA indisponível — lead {conv.get('nome') or mask_phone(phone)} aguardando resposta:\n\n{text}")
            return {"action": "llm_error"}

        # o corretor pode ter pausado a conversa pelo painel enquanto a IA pensava
        latest = _get(phone) or {}
        if latest.get("paused_by_broker"):
            conv["paused_by_broker"] = True
            conv["stage"] = latest.get("stage", "HUMAN_TAKEOVER")
            _upsert(phone, conv)
            return {"paused": True, "reason": "pausado durante a resposta"}

        result = _apply_decision(conv, decision, notify_broker)
        latest = _get(phone) or {}
        if latest.get("paused_by_broker") and not conv.get("paused_by_broker"):
            conv["paused_by_broker"] = True  # não desfaz pausa feita durante o envio
            conv["stage"] = "HUMAN_TAKEOVER"
        _upsert(phone, conv)
        return result


OPT_OUT_CONFIRMATION = os.getenv(
    "OPT_OUT_CONFIRMATION",
    "Tudo bem, você não receberá mais mensagens nossas. Desculpe o incômodo!",
)


def _handle_opt_out(phone: str, text: str, notify_broker: Callable[[str], None]) -> dict[str, Any]:
    already = storage.is_blocked(phone)
    storage.block(phone, "opt-out pelo lead")
    with _lock:
        conv = _get(phone) or {"phone": phone, "nome": "", "history": [], "collected": {}}
        conv["stage"] = "NOT_INTERESTED"
        conv["opted_out"] = True
        conv["paused_by_broker"] = True
        _append_history(conv, "user", text)
        storage.save_conversation(phone, conv)
    if not already:
        try:
            _evolution.send_text(phone, OPT_OUT_CONFIRMATION)
        except Exception:
            pass
        _close_not_interested(conv, "opt-out: pediu para não ser contatado")
        notify_broker(f"🚫 Lead {conv.get('nome') or mask_phone(phone)} pediu para sair. Bloqueado para sempre.")
    log.info("opt-out registrado", extra={"ctx": {"phone": mask_phone(phone)}})
    return {"action": "opt_out", "already_blocked": already}


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
        timeout=30,
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
        new_stage = _valid_stage(args.get("novo_estagio"), conv.get("stage", "ENGAGED"))
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
        new_stage = _valid_stage(args.get("novo_estagio"), conv.get("stage", "ENGAGED"))
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
        storage.block(conv["phone"], f"sem interesse: {motivo}"[:200])
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

    if when < datetime.now(TZ):
        # guarda nome/e-mail já informados para o lead não ter de repetir
        conv.setdefault("collected", {}).update({"nome_completo": nome_completo, "email": email})
        _send_outbound(conv, "Esse horário já passou 🙂 Pode me sugerir outro dia e horário?")
        conv["stage"] = "COLLECTING_DATETIME"
        return {"action": "agendar", "ok": False, "reason": "past_datetime"}

    if conv.get("stage") == "SCHEDULED" and conv.get("collected", {}).get("data_hora") == when.isoformat():
        # IA repetiu o agendamento do mesmo horário: não cria evento duplicado
        _send_outbound(conv, f"Sua reunião já está confirmada para {when.strftime('%d/%m/%Y às %H:%M')} 👍")
        return {"action": "agendar", "ok": True, "duplicate": True, "datetime": when.isoformat()}

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


def _valid_stage(stage: Any, fallback: str) -> str:
    stage = str(stage or "").strip().upper()
    return stage if stage in STAGES else (fallback or "ENGAGED")


def _sync_stage(conv: dict[str, Any]) -> None:
    """Só escreve na planilha quando o estágio muda (antes: 1 escrita por mensagem)."""
    sheet = conv.get("sheet")
    row = conv.get("row")
    stage = conv.get("stage")
    if sheet and row and stage and conv.get("sheet_stage") != stage:
        try:
            _sheets.update_stage(sheet, row, stage)
            conv["sheet_stage"] = stage
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
