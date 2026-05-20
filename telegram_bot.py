from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from llm_router import LLMRouter
from nlp_engine import NLPResult, parse_message
from sheet_manager import SheetManager

load_dotenv(Path(__file__).parent / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FLASK_BASE_URL = "http://127.0.0.1:5050"
CONTEXT_FILE = Path(__file__).parent / "bot_contexts.json"

CONFIDENCE_THRESHOLD = 0.4

INTENT_RESPONSES = {
    "fechou": [
        "Parabéns! 🎉 Vou registrar que o cliente fechou.",
        "Ótima notícia! 🎊 Atualizando o status agora.",
        "Excelente! ✅ Marcando como fechado na planilha.",
    ],
    "agendar_contato": [
        "Beleza, vou agendar o follow-up. ⏰",
        "Anotado! 📅 Vou programar o retorno.",
        "Perfeito! 📝 Registrando na planilha.",
    ],
    "sem_interesse": [
        "Entendi, vou registrar que o cliente não tem interesse por enquanto.",
        "Anotado. 📋 Marcando como sem interesse.",
        "Ok, registrando. Podemos retomar no futuro.",
    ],
    "em_negociacao": [
        "Certo! 🤝 Marcando como em negociação.",
        "Anotado! 💼 Registrando status de negociação.",
        "Ok, vou atualizar como em negociação.",
    ],
    "agendar_visita": [
        "Beleza! 📋 Registrando a reunião.",
        "Anotado! 📅 Agendando na planilha.",
    ],
    "enviar_proposta": [
        "Certo! 📄 Registrando que precisa enviar proposta.",
        "Anotado! 💰 Marcando para enviar orçamento.",
    ],
}

STATUS_MAP = {
    "fechou": "FECHADO ✅",
    "agendar_contato": "AGENDAR RETORNO ⏰",
    "sem_interesse": "SEM INTERESSE ❌",
    "em_negociacao": "EM NEGOCIAÇÃO 🤝",
    "agendar_visita": "REUNIÃO AGENDADA 📋",
    "enviar_proposta": "ENVIAR PROPOSTA 📄",
}


@dataclass
class ChatContext:
    chat_id: int = 0
    pending_action: str = ""
    pending_client_name: str = ""
    pending_candidates: list = field(default_factory=list)
    last_message_time: float = 0.0
    conversation_history: list = field(default_factory=list)

    def is_expired(self, timeout_seconds: float = 1800) -> bool:
        return time.time() - self.last_message_time > timeout_seconds

    def add_message(self, role: str, content: str) -> None:
        self.conversation_history.append({"role": role, "content": content, "time": time.time()})
        if len(self.conversation_history) > 20:
            self.conversation_history = self.conversation_history[-20:]
        self.last_message_time = time.time()


class ContextStore:
    def __init__(self, file_path: Path = CONTEXT_FILE) -> None:
        self.file_path = file_path
        self._contexts: dict[int, ChatContext] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self.file_path.exists():
            try:
                data = json.loads(self.file_path.read_text(encoding="utf-8"))
                for chat_id, ctx_data in data.items():
                    ctx = ChatContext(
                        chat_id=int(chat_id),
                        pending_action=ctx_data.get("pending_action", ""),
                        pending_client_name=ctx_data.get("pending_client_name", ""),
                        pending_candidates=ctx_data.get("pending_candidates", []),
                        last_message_time=ctx_data.get("last_message_time", 0),
                        conversation_history=ctx_data.get("conversation_history", []),
                    )
                    self._contexts[int(chat_id)] = ctx
            except Exception:
                pass

    def _save(self) -> None:
        data = {}
        for chat_id, ctx in self._contexts.items():
            data[chat_id] = {
                "pending_action": ctx.pending_action,
                "pending_client_name": ctx.pending_client_name,
                "pending_candidates": ctx.pending_candidates,
                "last_message_time": ctx.last_message_time,
                "conversation_history": ctx.conversation_history,
            }
        self.file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, chat_id: int) -> ChatContext:
        with self._lock:
            if chat_id not in self._contexts:
                self._contexts[chat_id] = ChatContext(chat_id=chat_id)
            ctx = self._contexts[chat_id]
            if ctx.is_expired():
                ctx = ChatContext(chat_id=chat_id)
                self._contexts[chat_id] = ctx
            return ctx

    def update(self, ctx: ChatContext) -> None:
        with self._lock:
            self._contexts[ctx.chat_id] = ctx
            self._save()

    def clear(self, chat_id: int) -> None:
        with self._lock:
            if chat_id in self._contexts:
                del self._contexts[chat_id]
                self._save()


class TelegramBot:
    def __init__(self, token: str, flask_url: str = FLASK_BASE_URL) -> None:
        self.token = token
        self.flask_url = flask_url
        self.application = None
        self._loop = None
        self.sheet_manager = SheetManager()
        self.context_store = ContextStore()
        self.llm = LLMRouter()

    def _api_get(self, path: str) -> dict | None:
        try:
            resp = requests.get(f"{self.flask_url}{path}", timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def _api_post(self, path: str, data: dict | None = None, files: dict | None = None) -> dict | None:
        try:
            resp = requests.post(f"{self.flask_url}{path}", data=data, files=files, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    async def _typing(self, update: Update) -> None:
        await update.effective_chat.send_action(action=ChatAction.TYPING)

    def _get_response(self, intent: str) -> str:
        import random
        responses = INTENT_RESPONSES.get(intent, ["Entendi! Vou registrar isso."])
        return random.choice(responses)

    def _format_candidate_list(self, candidates: list[dict]) -> str:
        lines = ["Encontrei esses possíveis clientes:\n"]
        for i, c in enumerate(candidates, 1):
            lines.append(f"{i}. *{c['nome']}* — {c['sheet']} (linha {c['row']}) — {c['score']:.0%} match")
        lines.append("\nResponda com o número do cliente correto ou digite o nome exato.")
        return "\n".join(lines)

    def _extract_client_name(self, text: str, fallback: str = "") -> str:
        words = text.split()
        for i, word in enumerate(words):
            if word[0].isupper() and len(word) > 2:
                if i + 1 < len(words) and words[i + 1][0].isupper():
                    return f"{word} {words[i + 1]}"
                if not any(kw in word.lower() for kw in ["fechou", "cliente", "proposta", "contato", "ligar", "seguro", "vida", "plano", "apólice", "status"]):
                    return word
        return fallback

    async def _confirm_update(self, update: Update, candidate: dict, nlp: NLPResult) -> None:
        status_label = STATUS_MAP.get(nlp.intent, nlp.intent.upper())
        notes = nlp.notes if nlp.notes else nlp.raw_message
        follow_up = ""

        if nlp.extracted_date:
            follow_up = nlp.extracted_date.date.strftime("%d/%m/%Y")
            notes = f"{notes} | Retorno: {follow_up}"

        result = self.sheet_manager.update_client_status(
            sheet_name=candidate["sheet"],
            row=candidate["row"],
            status=status_label,
            notes=notes,
            follow_up_date=follow_up,
        )

        if result.get("updated"):
            msg = f"✅ *Atualizado!*\n\n"
            msg += f"Cliente: *{candidate['nome']}*\n"
            msg += f"Status: {status_label}\n"
            if follow_up:
                msg += f"Retorno: {follow_up}\n"
            msg += f"Planilha: {candidate['sheet']} (linha {candidate['row']})"
            await update.message.reply_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Falha ao atualizar a planilha.")

    async def _update_status_flow(self, update: Update, ctx: ChatContext, text: str, nlp: NLPResult) -> None:
        client_name = self._extract_client_name(text, fallback=ctx.pending_client_name)

        if not client_name:
            await update.message.reply_text("Qual o nome do cliente?")
            ctx.pending_action = text
            self.context_store.update(ctx)
            return

        candidates = self.sheet_manager.find_client(client_name)

        if not candidates:
            await update.message.reply_text(f"Nenhum cliente encontrado com o nome '{client_name}'.")
            self.context_store.clear(ctx.chat_id)
            return

        if len(candidates) == 1 and candidates[0]["score"] > 0.85:
            response = self._get_response(nlp.intent)
            await update.message.reply_text(response)
            await self._confirm_update(update, candidates[0], nlp)
            self.context_store.clear(ctx.chat_id)
            return

        ctx.pending_action = text
        ctx.pending_client_name = client_name
        ctx.pending_candidates = candidates
        self.context_store.update(ctx)
        await update.message.reply_text(self._format_candidate_list(candidates), parse_mode="Markdown")

    async def _consultar_cliente(self, update: Update, nome: str) -> None:
        await self._typing(update)
        if not nome:
            await update.message.reply_text("Qual cliente?")
            return
        details = self.sheet_manager.get_client_details(nome)
        if not details["found"]:
            await update.message.reply_text(f"Nenhum cliente encontrado com '{nome}'.")
            return
        best = details["best"]
        full = details["full_row"] or {}
        msg = f"👤 *{best['nome']}*\n"
        msg += f"Planilha: {best['sheet']} (linha {best['row']})\n"
        if full.get("telefone") or full.get("telefone_planilha"):
            msg += f"📞 Telefone: {full.get('telefone') or full.get('telefone_planilha')}\n"
        if full.get("email"):
            msg += f"📧 Email: {full.get('email')}\n"
        if full.get("status"):
            msg += f"📌 Status: {full.get('status')}\n"
        if full.get("follow-up"):
            msg += f"⏰ Retorno: {full.get('follow-up')}\n"
        if full.get("notas"):
            notas = full.get("notas")[:300]
            msg += f"\n📝 Notas:\n{notas}"
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _listar_followups(self, update: Update, periodo: str = "today") -> None:
        await self._typing(update)
        items = self.sheet_manager.list_followups(when=periodo)
        if not items:
            label = {"today": "hoje", "week": "essa semana", "overdue": "atrasados"}.get(periodo, periodo)
            await update.message.reply_text(f"Nenhum follow-up {label}.")
            return
        label = {"today": "hoje", "week": "essa semana", "overdue": "atrasados"}.get(periodo, periodo)
        lines = [f"📅 *Follow-ups {label}* ({len(items)}):\n"]
        for it in items[:15]:
            data_str = it["follow_up_date"].strftime("%d/%m")
            tel = f" — 📞 {it['telefone']}" if it["telefone"] else ""
            lines.append(f"• {data_str} — *{it['nome']}*{tel}")
        if len(items) > 15:
            lines.append(f"\n…e mais {len(items) - 15}.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _estatisticas(self, update: Update, periodo: str = "month") -> None:
        await self._typing(update)
        stats = self.sheet_manager.get_stats(period=periodo)
        label = {"day": "hoje", "week": "esta semana", "month": "este mês"}.get(periodo, periodo)
        msg = f"📊 *Estatísticas — {label}*\n\n"
        msg += f"Total geral de leads com status: {stats['total']}\n"
        msg += f"Atividade no período: {stats['recent_total']}\n"
        if stats["recent_total"] > 0:
            msg += f"Taxa de conversão: {stats['conversion_rate']:.0%}\n"
        msg += "\n*Por status (geral):*\n"
        for bucket, count in sorted(stats["by_status"].items(), key=lambda x: -x[1]):
            msg += f"• {bucket}: {count}\n"
        if stats["recent_by_status"]:
            msg += f"\n*No período ({label}):*\n"
            for bucket, count in sorted(stats["recent_by_status"].items(), key=lambda x: -x[1]):
                msg += f"• {bucket}: {count}\n"
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _adicionar_cliente(self, update: Update, args: dict) -> None:
        nome = args.get("nome", "").strip()
        aba = args.get("aba", "").strip()
        if not nome:
            await update.message.reply_text("Preciso do nome pra adicionar o cliente.")
            return
        if not aba:
            sheets = self.sheet_manager.list_sheets()
            await update.message.reply_text(
                f"Em qual aba? Disponíveis: {', '.join(sheets)}"
            )
            return
        result = self.sheet_manager.add_client(
            sheet_name=aba,
            nome=nome,
            telefone=args.get("telefone", ""),
            email=args.get("email", ""),
            notas=args.get("notas", ""),
        )
        if result.get("added"):
            await update.message.reply_text(f"✅ Cliente *{nome}* adicionado na aba *{aba}*.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ {result.get('error', 'Falha ao adicionar.')}")

    async def _verificar_e_adicionar_clientes(self, update: Update, args: dict) -> None:
        clientes = args.get("clientes", [])
        aba_padrao = args.get("aba_padrao", "").strip()
        if not clientes:
            await update.message.reply_text("Não consegui extrair clientes da imagem.")
            return

        sheets = self.sheet_manager.list_sheets()
        if not aba_padrao and sheets:
            aba_padrao = sheets[0]

        adicionados: list[str] = []
        existentes: list[dict] = []
        falhas: list[str] = []

        for cliente in clientes:
            nome = cliente.get("nome", "").strip()
            if not nome:
                continue
            candidates = self.sheet_manager.find_client(nome, threshold=0.75, max_results=3)
            if candidates and candidates[0]["score"] > 0.75:
                existentes.append({
                    "nome": nome,
                    "sheet": candidates[0]["sheet"],
                    "row": candidates[0]["row"],
                })
            else:
                result = self.sheet_manager.add_client(
                    sheet_name=cliente.get("aba", aba_padrao),
                    nome=nome,
                    telefone=cliente.get("telefone", ""),
                    email=cliente.get("email", ""),
                    notas=cliente.get("notas", ""),
                )
                if result.get("added"):
                    adicionados.append(nome)
                else:
                    falhas.append(f"{nome}: {result.get('error', 'Erro desconhecido')}")

        msg_lines: list[str] = []
        if adicionados:
            msg_lines.append(f"✅ *Adicionados ({len(adicionados)}):*\n" + "\n".join(f"• {n}" for n in adicionados))
        if existentes:
            msg_lines.append(
                f"📋 *Já existentes ({len(existentes)}):*\n"
                + "\n".join(f"• {c['nome']} — {c['sheet']} (linha {c['row']})" for c in existentes)
            )
        if falhas:
            msg_lines.append(f"❌ *Falhas ({len(falhas)}):*\n" + "\n".join(f"• {f}" for f in falhas))

        await update.message.reply_text(
            "\n\n".join(msg_lines) or "Nenhuma ação realizada.",
            parse_mode="Markdown",
        )

    async def _listar_abas(self, update: Update) -> None:
        await self._typing(update)
        sheets = self.sheet_manager.list_sheets()
        await update.message.reply_text("📑 *Abas disponíveis:*\n" + "\n".join(f"• {s}" for s in sheets), parse_mode="Markdown")

    async def _status_job(self, update: Update) -> None:
        await self._typing(update)
        data = self._api_get("/job")
        if not data:
            await update.message.reply_text("Falha ao consultar job.")
            return
        job = data.get("job") or data.get("last_job")
        if not job:
            await update.message.reply_text("Nenhum job encontrado.")
            return
        status = job.get("status", "unknown")
        emoji = {"running": "🔄", "completed": "✅", "stopped": "⏹️", "failed": "❌"}.get(status, "❓")
        text = f"{emoji} Job: {status.upper()}\n{job.get('message', '')}"
        progress = job.get("progress", {})
        if progress.get("total", 0) > 0:
            text += f"\nProgresso: {progress['current']}/{progress['total']}"
        await update.message.reply_text(text)

    async def _iniciar_scraping(self, update: Update, limite: int | None) -> None:
        await self._typing(update)
        data = {"limit": str(limite)} if limite else {}
        result = self._api_post("/run", data=data)
        if result and result.get("ok"):
            await update.message.reply_text("🚀 Scraping iniciado. Use 'como tá o job' pra acompanhar.")
        else:
            error = result.get("error", "Falha") if result else "Servidor indisponível"
            await update.message.reply_text(f"❌ {error}")

    async def _parar_scraping(self, update: Update) -> None:
        await self._typing(update)
        result = self._api_post("/stop")
        if result and result.get("ok"):
            await update.message.reply_text("⏹️ Job parado.")
        else:
            await update.message.reply_text("Nenhum job em execução.")

    async def _iniciar_disparo(self, update: Update, args: dict) -> None:
        await self._typing(update)
        agora = bool(args.get("agora", False))
        result = self._api_post("/outreach/start")
        if not result or not result.get("ok"):
            await update.message.reply_text("❌ Falha ao iniciar disparo.")
            return
        msg = "🚀 Disparo ligado."
        if agora:
            now_result = self._api_post("/outreach/dispatch-now")
            if now_result and now_result.get("sent"):
                lead = now_result.get("lead", {})
                msg += f"\n📤 Enviado para *{lead.get('nome', '?')}* ({lead.get('telefone', '?')})."
            elif now_result and now_result.get("empty"):
                msg += "\n⚠️ Fila vazia — nenhum lead pendente."
            else:
                err = (now_result or {}).get("error", "desconhecido")
                msg += f"\n⚠️ Envio imediato falhou: {err}"
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _parar_disparo(self, update: Update) -> None:
        await self._typing(update)
        result = self._api_post("/outreach/stop")
        if result and result.get("ok"):
            await update.message.reply_text("⏹️ Disparo desligado.")
        else:
            await update.message.reply_text("❌ Falha ao parar disparo.")

    async def _status_disparo(self, update: Update) -> None:
        await self._typing(update)
        data = self._api_get("/outreach/status")
        if not data:
            await update.message.reply_text("Falha ao consultar status.")
            return
        emoji = "🟢" if data.get("enabled") else "🔴"
        lines = [f"{emoji} *Disparo:* {'ligado' if data.get('enabled') else 'desligado'}"]
        if data.get("paused_reason"):
            lines.append(f"⏸️ Pausa: {data['paused_reason']}")
        lines.append(f"📤 Enviados hoje: {data.get('sent_today', 0)}/{data.get('daily_limit', '?')}")
        lines.append(f"📋 Fila: {data.get('queue_size', '?')} leads pendentes")
        lines.append(f"⏱️ Janela: {'dentro' if data.get('in_window') else 'fora'}")
        if data.get("next_dispatch_in_seconds") is not None:
            lines.append(f"➡️ Próximo envio: {data['next_dispatch_in_seconds']}s")
        if data.get("last_error"):
            lines.append(f"⚠️ Último erro: {data['last_error'][:120]}")
        if not data.get("evolution_configured"):
            lines.append("⚠️ Evolution não configurado (.env)")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _listar_conversas(self, update: Update) -> None:
        await self._typing(update)
        data = self._api_get("/outreach/conversations")
        convs = (data or {}).get("conversations", [])
        if not convs:
            await update.message.reply_text("Nenhuma conversa em andamento.")
            return
        lines = [f"💬 *Conversas ativas* ({len(convs)}):\n"]
        for c in convs[:20]:
            badge = "⏸️" if c.get("paused_by_broker") else "🤖"
            nome = c.get("nome") or c.get("phone")
            lines.append(f"{badge} *{nome}* — `{c.get('stage', '?')}`")
            preview = c.get("last_message_preview")
            if preview:
                lines.append(f"   _{preview}_")
        if len(convs) > 20:
            lines.append(f"\n…e mais {len(convs) - 20}.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _pausar_lead(self, update: Update, args: dict, pause: bool) -> None:
        await self._typing(update)
        nome = (args.get("nome") or "").strip()
        telefone = (args.get("telefone") or "").strip()
        phone = telefone
        if not phone and nome:
            details = self.sheet_manager.get_client_details(nome)
            if details.get("found"):
                full = details.get("full_row") or {}
                phone = full.get("telefone") or full.get("telefone_planilha") or ""
        if not phone:
            await update.message.reply_text("Preciso do nome ou do telefone do lead.")
            return
        path = "pause" if pause else "resume"
        result = self._api_post(f"/outreach/conversations/{phone}/{path}")
        if result and result.get("ok"):
            verbo = "pausada" if pause else "retomada"
            await update.message.reply_text(f"✅ Conversa {verbo} para {nome or phone}.")
        else:
            await update.message.reply_text("❌ Não encontrei conversa para este lead.")

    async def _proxima_agenda(self, update: Update, args: dict) -> None:
        await self._typing(update)
        periodo = args.get("periodo", "hoje")
        try:
            from calendar_manager import CalendarManager

            events = CalendarManager().list_upcoming(max_results=15)
        except Exception as exc:
            await update.message.reply_text(f"❌ Falha ao consultar Calendar: {exc}")
            return
        if not events:
            await update.message.reply_text("Nenhuma reunião próxima no Calendar.")
            return
        from datetime import datetime as _dt

        now = _dt.now().date()
        lines = [f"📅 *Próximas reuniões ({periodo}):*\n"]
        for ev in events:
            start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
            if not start:
                continue
            try:
                dt = _dt.fromisoformat(start.replace("Z", "+00:00"))
            except Exception:
                continue
            if periodo == "hoje" and dt.date() != now:
                continue
            attendees = ev.get("attendees", []) or []
            lead_email = next((a.get("email") for a in attendees if a.get("email")), "")
            lines.append(f"• {dt.strftime('%d/%m %H:%M')} — *{ev.get('summary', 'Reunião')}* {lead_email}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    def send_broker_notification(self, text: str) -> None:
        if not self.application or not self._loop:
            return
        chat_id = next(iter(self.context_store._contexts.keys()), None)
        if not chat_id:
            return
        import asyncio

        try:
            asyncio.run_coroutine_threadsafe(
                self.application.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown"),
                self._loop,
            )
        except Exception:
            pass

    async def _execute_tool(self, update: Update, ctx: ChatContext, tool: str, args: dict) -> None:
        if tool == "atualizar_status_cliente":
            text = f"{args.get('nome', '')} {args.get('status', '')}"
            if args.get("notas"):
                text += f" {args['notas']}"
            if args.get("data_retorno"):
                text += f" retorno {args['data_retorno']}"
            nlp = parse_message(text)
            if nlp.intent == "unknown":
                nlp.intent = args.get("status", "unknown")
            await self._update_status_flow(update, ctx, text, nlp)
            return
        if tool == "consultar_cliente":
            await self._consultar_cliente(update, args.get("nome", ""))
            return
        if tool == "listar_followups":
            await self._listar_followups(update, args.get("periodo", "today"))
            return
        if tool == "estatisticas":
            await self._estatisticas(update, args.get("periodo", "month"))
            return
        if tool == "adicionar_cliente":
            await self._adicionar_cliente(update, args)
            return
        if tool == "verificar_e_adicionar_clientes":
            await self._verificar_e_adicionar_clientes(update, args)
            return
        if tool == "iniciar_scraping":
            await self._iniciar_scraping(update, args.get("limite"))
            return
        if tool == "parar_scraping":
            await self._parar_scraping(update)
            return
        if tool == "status_job":
            await self._status_job(update)
            return
        if tool == "listar_abas":
            await self._listar_abas(update)
            return
        if tool == "iniciar_disparo":
            await self._iniciar_disparo(update, args)
            return
        if tool == "parar_disparo":
            await self._parar_disparo(update)
            return
        if tool == "status_disparo":
            await self._status_disparo(update)
            return
        if tool == "listar_conversas":
            await self._listar_conversas(update)
            return
        if tool == "pausar_lead":
            await self._pausar_lead(update, args, pause=True)
            return
        if tool == "retomar_lead":
            await self._pausar_lead(update, args, pause=False)
            return
        if tool == "proxima_agenda":
            await self._proxima_agenda(update, args)
            return
        if tool == "responder_texto":
            await update.message.reply_text(args.get("mensagem", "Não entendi."))
            return
        await update.message.reply_text("Não consegui executar essa ação.")

    async def _handle_client_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE, ctx: ChatContext) -> None:
        text = update.message.text.strip()

        if text.lower() in ("cancelar", "cancela", "sair"):
            self.context_store.clear(ctx.chat_id)
            await update.message.reply_text("Operação cancelada.")
            return

        if text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(ctx.pending_candidates):
                candidate = ctx.pending_candidates[idx]
                nlp = parse_message(ctx.pending_action)
                await self._confirm_update(update, candidate, nlp)
                self.context_store.clear(ctx.chat_id)
                return
            else:
                await update.message.reply_text("Número inválido. Tente novamente ou digite /cancelar.")
                return

        name = text
        candidates = self.sheet_manager.find_client(name)
        if not candidates:
            await update.message.reply_text(f"Nenhum cliente encontrado com o nome '{name}'. Tente outro nome ou /cancelar.")
            ctx.pending_client_name = name
            self.context_store.update(ctx)
            return

        if len(candidates) == 1 and candidates[0]["score"] > 0.85:
            nlp = parse_message(ctx.pending_action)
            await self._confirm_update(update, candidates[0], nlp)
            self.context_store.clear(ctx.chat_id)
            return

        ctx.pending_candidates = candidates
        self.context_store.update(ctx)
        await update.message.reply_text(self._format_candidate_list(candidates), parse_mode="Markdown")

    def _rule_based_route(self, nlp: NLPResult, text: str) -> dict | None:
        normalized = text.lower()
        if any(kw in normalized for kw in ("disparar", "disparo", "iniciar envio", "começar envio", "comecar envio")):
            if "parar" in normalized or "desligar" in normalized or "stop" in normalized or "pausa" in normalized:
                return {"tool": "parar_disparo", "args": {}}
            if "status" in normalized or "como tá" in normalized or "como ta" in normalized or "andamento" in normalized:
                return {"tool": "status_disparo", "args": {}}
            agora = "agora" in normalized or "imediato" in normalized or "já" in normalized or "ja " in normalized
            return {"tool": "iniciar_disparo", "args": {"agora": agora}}
        if "conversa" in normalized and ("ativa" in normalized or "andamento" in normalized or "abertas" in normalized or "listar" in normalized):
            return {"tool": "listar_conversas", "args": {}}
        if normalized.startswith("agenda") or "próximas reuniões" in normalized or "proximas reunioes" in normalized:
            periodo = "hoje" if "hoje" in normalized else "semana"
            return {"tool": "proxima_agenda", "args": {"periodo": periodo}}

        if nlp.confidence < CONFIDENCE_THRESHOLD or nlp.intent == "unknown":
            return None

        intent = nlp.intent
        if intent in STATUS_MAP:
            return {"tool": "atualizar_status_cliente", "args": {"nome": "", "status": intent}}
        if intent == "consultar_cliente":
            nome = self._extract_client_name(text)
            return {"tool": "consultar_cliente", "args": {"nome": nome}}
        if intent == "listar_followups":
            normalized = text.lower()
            if "semana" in normalized:
                periodo = "week"
            elif "atras" in normalized or "venci" in normalized:
                periodo = "overdue"
            else:
                periodo = "today"
            return {"tool": "listar_followups", "args": {"periodo": periodo}}
        if intent == "estatisticas":
            normalized = text.lower()
            if "semana" in normalized:
                periodo = "week"
            elif "hoje" in normalized or "dia" in normalized:
                periodo = "day"
            else:
                periodo = "month"
            return {"tool": "estatisticas", "args": {"periodo": periodo}}
        if intent == "adicionar_cliente":
            return None
        if intent == "comando_scraping":
            normalized = text.lower()
            if "parar" in normalized or "interromper" in normalized or "stop" in normalized:
                return {"tool": "parar_scraping", "args": {}}
            if "status" in normalized or "andamento" in normalized or "como tá" in normalized or "como ta" in normalized:
                return {"tool": "status_job", "args": {}}
            return {"tool": "iniciar_scraping", "args": {}}
        return None

    async def _process_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text.strip()
        chat_id = update.effective_chat.id
        ctx = self.context_store.get(chat_id)

        await self._typing(update)

        ctx.add_message("user", text)

        if ctx.pending_action:
            await self._handle_client_selection(update, context, ctx)
            return

        nlp = parse_message(text)
        route = self._rule_based_route(nlp, text)

        if not route:
            llm_route = self.llm.route(text, history=ctx.conversation_history)
            route = {"tool": llm_route["tool"], "args": llm_route["args"]}

        tool = route["tool"]
        args = route["args"]

        if tool == "atualizar_status_cliente":
            if not args.get("nome"):
                args["nome"] = self._extract_client_name(text)
            status_intent = args.get("status", nlp.intent)
            full_text = text
            if args.get("notas"):
                full_text = f"{full_text} {args['notas']}"
            if args.get("data_retorno"):
                full_text = f"{full_text} retorno {args['data_retorno']}"
            inner_nlp = parse_message(full_text)
            if inner_nlp.intent == "unknown" or inner_nlp.confidence < 0.2:
                inner_nlp.intent = status_intent
            await self._update_status_flow(update, ctx, full_text if args.get("nome") else text, inner_nlp)
            ctx.add_message("bot", f"[tool] {tool}")
            self.context_store.update(ctx)
            return

        await self._execute_tool(update, ctx, tool, args)
        ctx.add_message("bot", f"[tool] {tool}")
        self.context_store.update(ctx)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._typing(update)
        self.context_store.clear(update.effective_chat.id)
        menu = (
            "Bem-vindo à Verona Corretora! 🛡️\n\n"
            "Sou seu assistente — converse comigo naturalmente.\n\n"
            "*Atualizar cliente:*\n"
            "• _João Silva fechou a proposta_\n"
            "• _Maria pediu pra ligar daqui 2 meses_\n\n"
            "*Consultar:*\n"
            "• _Como tá o Pedro?_\n"
            "• _Telefone da Ana_\n\n"
            "*Agenda:*\n"
            "• _Quem tenho que ligar hoje?_\n"
            "• _Follow-ups da semana_\n"
            "• _Retornos atrasados_\n\n"
            "*Métricas:*\n"
            "• _Quantos fechei esse mês?_\n"
            "• _Resumo da semana_\n\n"
            "*Scraper:*\n"
            "• _Iniciar coleta de 10 leads_\n"
            "• _Como tá o job?_ / _Parar coleta_\n\n"
            "*Cadastro:*\n"
            "• _Adiciona Maria Souza, telefone 48999..._\n\n"
            "*Imagens:*\n"
            "• Envie uma foto com legenda pedindo ações\n"
            "• _Verifica se esses clientes tão na planilha_\n\n"
            "Comandos: /help /status /job /abas /cancelar"
        )
        await update.message.reply_text(menu, parse_mode="Markdown")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.cmd_start(update, context)

    async def cmd_cancelar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.context_store.clear(update.effective_chat.id)
        await update.message.reply_text("Operação cancelada.")

    async def cmd_abas(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._typing(update)
        await self._listar_abas(update)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._typing(update)
        await update.message.reply_text("Verificando status...")
        data = self._api_get("/status")
        if not data:
            await update.message.reply_text("Falha ao conectar com o servidor.")
            return
        if data.get("connected"):
            text = (
                "Conexão OK\n\n"
                f"Planilha: {data.get('spreadsheet_title', 'N/A')}\n"
                f"ID: {data.get('spreadsheet_id', 'N/A')}\n"
                f"Abas: {len(data.get('sheets', []))}\n"
                f"Leads disponíveis: {data.get('total_white_rows', 'N/A')}"
            )
        else:
            text = f"Desconectado\n{data.get('message', 'Sem detalhes')}"
        await update.message.reply_text(text)

    async def cmd_job(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._typing(update)
        await self._status_job(update)

    async def cmd_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._typing(update)
        args = " ".join(context.args).strip() if context.args else ""
        limite = None
        parts = args.split()
        if len(parts) == 1 and parts[0].isdigit():
            limite = int(parts[0])
        await self._iniciar_scraping(update, limite)

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._typing(update)
        await self._parar_scraping(update)

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        ctx = self.context_store.get(chat_id)
        caption = (update.message.caption or "").strip()

        await self._typing(update)

        # Baixa a imagem na melhor resolução disponível
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        b64 = base64.b64encode(image_bytes).decode("utf-8")

        prompt = caption if caption else "O usuário enviou esta imagem. Extraia as informações e execute a ação solicitada."
        route = self.llm.route_vision(prompt, b64, history=ctx.conversation_history)

        tool = route["tool"]
        args = route["args"]

        await self._execute_tool(update, ctx, tool, args)
        ctx.add_message("bot", f"[tool] {tool}")
        self.context_store.update(ctx)

    async def cmd_disparar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args_text = " ".join(context.args) if context.args else ""
        agora = "agora" in args_text.lower() or "now" in args_text.lower()
        await self._iniciar_disparo(update, {"agora": agora})

    async def cmd_pausar_disparo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._parar_disparo(update)

    async def cmd_status_disparo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._status_disparo(update)

    async def cmd_conversas(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._listar_conversas(update)

    async def cmd_agenda(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._proxima_agenda(update, {"periodo": "semana"})

    async def cmd_pausar_lead(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        nome = " ".join(context.args).strip() if context.args else ""
        await self._pausar_lead(update, {"nome": nome}, pause=True)

    async def cmd_retomar_lead(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        nome = " ".join(context.args).strip() if context.args else ""
        await self._pausar_lead(update, {"nome": nome}, pause=False)

    def _run_bot(self) -> None:
        import asyncio

        try:
            self._loop = asyncio.get_event_loop()
            if self._loop.is_closed():
                raise RuntimeError("closed loop")
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

        request = HTTPXRequest(httpx_kwargs={"verify": False})
        self.application = (
            Application.builder()
            .token(self.token)
            .request(request)
            .build()
        )
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("help", self.cmd_help))
        self.application.add_handler(CommandHandler("status", self.cmd_status))
        self.application.add_handler(CommandHandler("job", self.cmd_job))
        self.application.add_handler(CommandHandler("run", self.cmd_run))
        self.application.add_handler(CommandHandler("stop", self.cmd_stop))
        self.application.add_handler(CommandHandler("cancelar", self.cmd_cancelar))
        self.application.add_handler(CommandHandler("abas", self.cmd_abas))
        self.application.add_handler(CommandHandler("disparar", self.cmd_disparar))
        self.application.add_handler(CommandHandler("parar_disparo", self.cmd_pausar_disparo))
        self.application.add_handler(CommandHandler("status_disparo", self.cmd_status_disparo))
        self.application.add_handler(CommandHandler("conversas", self.cmd_conversas))
        self.application.add_handler(CommandHandler("agenda", self.cmd_agenda))
        self.application.add_handler(CommandHandler("pausar_lead", self.cmd_pausar_lead))
        self.application.add_handler(CommandHandler("retomar_lead", self.cmd_retomar_lead))
        self.application.add_handler(MessageHandler(filters.PHOTO, self._handle_photo))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._process_message))
        self.application.run_polling(drop_pending_updates=True)

    def start(self) -> None:
        self._run_bot()
