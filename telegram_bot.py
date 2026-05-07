from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from nlp_engine import NLPResult, parse_message
from sheet_manager import SheetManager

TELEGRAM_BOT_TOKEN = "8457276789:AAHVPjx_-DVEDFrXSmd4Bd5S37grpskdnHE"
FLASK_BASE_URL = "http://127.0.0.1:5050"
CONTEXT_FILE = Path(__file__).parent / "bot_contexts.json"

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
        self.sheet_manager = SheetManager()
        self.context_store = ContextStore()

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

    async def _process_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text.strip()
        chat_id = update.effective_chat.id
        ctx = self.context_store.get(chat_id)

        ctx.add_message("user", text)

        if ctx.pending_action:
            await self._handle_client_selection(update, context, ctx)
            return

        nlp = parse_message(text)

        if nlp.intent == "unknown" or nlp.confidence < 0.3:
            ctx.add_message("bot", "Não entendi. Use /help para ver os comandos.")
            await update.message.reply_text("Não entendi. Use /help para ver os comandos ou descreva o que aconteceu com o cliente.")
            self.context_store.update(ctx)
            return

        client_name = ""
        words = text.split()
        for i, word in enumerate(words):
            if word[0].isupper() and len(word) > 2:
                if i + 1 < len(words) and words[i + 1][0].isupper():
                    client_name = f"{word} {words[i + 1]}"
                    break
                elif not any(kw in word.lower() for kw in ["fechou", "cliente", "proposta", "contato", "ligar", "seguro", "vida", "plano", "apólice"]):
                    client_name = word
                    break

        if not client_name and ctx.pending_client_name:
            client_name = ctx.pending_client_name

        if not client_name:
            await update.message.reply_text("Qual o nome do cliente?")
            ctx.pending_action = text
            self.context_store.update(ctx)
            return

        candidates = self.sheet_manager.find_client(client_name)

        if not candidates:
            await update.message.reply_text(f"Nenhum cliente encontrado com o nome '{client_name}'. Verifique o nome ou tente de outra forma.")
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

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.context_store.clear(update.effective_chat.id)
        menu = (
            "Bem-vindo à Verona Corretora! 🛡️\n\n"
            "Sou seu assistente de gestão de clientes. "
            "Basta me contar o que aconteceu que eu atualizo as planilhas!\n\n"
            "Exemplos:\n"
            "• _'João Silva fechou a proposta'_\n"
            "• _'Maria pediu pra ligar daqui 2 meses'_\n"
            "• _'Carlos não tem interesse agora'_\n"
            "• _'Ana quer agendar uma reunião semana que vem'_\n\n"
            "Comandos:\n"
            "/status — Status do sistema\n"
            "/job — Job atual\n"
            "/run — Iniciar scraping\n"
            "/stop — Parar job\n"
            "/help — Ajuda"
        )
        await update.message.reply_text(menu, parse_mode="Markdown")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "Como usar:\n\n"
            "Basta escrever naturalmente o que aconteceu com o cliente. "
            "Eu identifico o nome, a intenção e atualizo a planilha.\n\n"
            "Exemplos:\n"
            "• _'Pedro Santos fechou o seguro de vida'_\n"
            "• _'Lucia quer que eu ligue mês que vem'_\n"
            "• _'Marcos não quer mais, mandou embora'_\n"
            "• _'Fernanda está analisando a proposta'_\n\n"
            "Se eu encontrar mais de um cliente com nome parecido, "
            "vou pedir pra você escolher o correto.\n\n"
            "Comandos:\n"
            "/status — Verifica conexão Google Sheets\n"
            "/job — Status do job atual\n"
            "/run — Inicia scraping\n"
            "/stop — Para job\n"
            "/help — Esta mensagem"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await update.message.reply_text("Consultando job...")
        data = self._api_get("/job")
        if not data:
            await update.message.reply_text("Falha ao consultar job.")
            return
        job = data.get("job") or data.get("last_job")
        if not job:
            await update.message.reply_text("Nenhum job encontrado.")
            return
        status = job.get("status", "unknown")
        status_emoji = {"running": "🔄", "completed": "✅", "stopped": "⏹️", "failed": "❌"}.get(status, "❓")
        text = f"{status_emoji} Job: {status.upper()}\n"
        text += f"ID: {job.get('job_id', 'N/A')}\n"
        text += f"Mensagem: {job.get('message', 'N/A')}\n"
        progress = job.get("progress", {})
        if progress.get("total", 0) > 0:
            text += f"Progresso: {progress['current']}/{progress['total']}\n"
        summary = job.get("summary", {})
        if summary:
            found = summary.get("found", job.get("found", 0))
            processed = summary.get("processed", 0)
            text += f"Processados: {processed} | Encontrados: {found}\n"
        logs = job.get("logs", [])[-5:]
        if logs:
            text += "\nÚltimos logs:\n" + "\n".join(f"• {l}" for l in logs)
        await update.message.reply_text(text)

    async def cmd_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = " ".join(context.args).strip() if context.args else ""
        data = {}
        parts = args.split()
        if len(parts) == 1 and parts[0].isdigit():
            data["limit"] = parts[0]
        elif len(parts) >= 4:
            data["spreadsheet_id"] = parts[0]
            data["output_sheet_name"] = parts[1]
            data["seccional"] = parts[2]
            data["limit"] = parts[3]
        elif len(parts) == 1:
            data["spreadsheet_id"] = parts[0]
        await update.message.reply_text("Iniciando scraping...")
        result = self._api_post("/run", data=data)
        if result and result.get("ok"):
            await update.message.reply_text("Job iniciado! Use /job para acompanhar.")
        else:
            error = result.get("error", "Falha desconhecida") if result else "Servidor indisponível"
            await update.message.reply_text(f"Erro: {error}")

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        result = self._api_post("/stop")
        if result and result.get("ok"):
            await update.message.reply_text("Job parado.")
        else:
            await update.message.reply_text("Nenhum job em execução.")

    def _run_bot(self) -> None:
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
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._process_message))
        self.application.run_polling(drop_pending_updates=True)

    def start(self) -> None:
        self._run_bot()
