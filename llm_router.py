from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = """Você é o assistente da Verona Corretora, ajudando o corretor a gerenciar clientes via Telegram.

Você tem acesso a ferramentas para manipular a planilha do Google Sheets e o scraper de leads.
Quando o corretor descrever uma ação ou pedir informação, escolha a ferramenta correta e chame-a.
Se faltar contexto crítico (ex: nome do cliente), pergunte antes de chamar a ferramenta.

Histórico recente da conversa é fornecido. Responda sempre em português brasileiro, de forma curta e direta.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "atualizar_status_cliente",
            "description": "Atualiza o status de um cliente existente na planilha (ex: fechou, sem interesse, em negociação, agendar retorno).",
            "parameters": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string", "description": "Nome do cliente"},
                    "status": {
                        "type": "string",
                        "enum": ["fechou", "agendar_contato", "sem_interesse", "em_negociacao", "agendar_visita", "enviar_proposta"],
                    },
                    "notas": {"type": "string", "description": "Observação livre sobre o cliente"},
                    "data_retorno": {"type": "string", "description": "Data de follow-up no formato DD/MM/YYYY (opcional)"},
                },
                "required": ["nome", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_cliente",
            "description": "Busca informações de um cliente na planilha (status, telefone, notas, etc).",
            "parameters": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string"},
                },
                "required": ["nome"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_followups",
            "description": "Lista clientes com retorno agendado em um período (hoje, semana, ou vencidos).",
            "parameters": {
                "type": "object",
                "properties": {
                    "periodo": {"type": "string", "enum": ["today", "week", "overdue"]},
                },
                "required": ["periodo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estatisticas",
            "description": "Retorna estatísticas de clientes (totais por status, taxa de conversão).",
            "parameters": {
                "type": "object",
                "properties": {
                    "periodo": {"type": "string", "enum": ["day", "week", "month"]},
                },
                "required": ["periodo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adicionar_cliente",
            "description": "Adiciona um novo cliente em uma aba da planilha.",
            "parameters": {
                "type": "object",
                "properties": {
                    "aba": {"type": "string", "description": "Nome da aba"},
                    "nome": {"type": "string"},
                    "telefone": {"type": "string"},
                    "email": {"type": "string"},
                    "notas": {"type": "string"},
                },
                "required": ["aba", "nome"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "iniciar_scraping",
            "description": "Inicia coleta de leads na OAB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limite": {"type": "integer", "description": "Quantidade máxima de leads"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parar_scraping",
            "description": "Interrompe job de scraping em execução.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "status_job",
            "description": "Verifica andamento do job de scraping atual.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_abas",
            "description": "Lista as abas (sheets) disponíveis na planilha.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verificar_e_adicionar_clientes",
            "description": "Quando o usuário envia uma imagem ou lista com vários clientes, verifica quais já existem na planilha e adiciona os novos. Use quando houver múltiplos nomes (ex: lista de advogados, planilha fotografada).",
            "parameters": {
                "type": "object",
                "properties": {
                    "clientes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "nome": {"type": "string"},
                                "telefone": {"type": "string"},
                                "email": {"type": "string"},
                                "notas": {"type": "string"},
                                "aba": {"type": "string"},
                            },
                            "required": ["nome"],
                        },
                    },
                    "aba_padrao": {"type": "string", "description": "Aba padrão para adicionar novos clientes se não especificado"},
                },
                "required": ["clientes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "iniciar_disparo",
            "description": "Liga o disparo automático de primeiras mensagens via WhatsApp. Pode opcionalmente forçar 1 envio imediato.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agora": {"type": "boolean", "description": "Se true, dispara 1 mensagem imediatamente além de ligar o worker."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parar_disparo",
            "description": "Desliga o disparo automático.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "status_disparo",
            "description": "Mostra status atual do disparo (ligado/desligado, enviados hoje, fila, próximo envio).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_conversas",
            "description": "Lista conversas em andamento via WhatsApp, com estágio e última mensagem.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pausar_lead",
            "description": "Pausa o bot para um lead específico (corretor humano assume). Use quando o corretor pedir.",
            "parameters": {
                "type": "object",
                "properties": {"nome": {"type": "string"}, "telefone": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retomar_lead",
            "description": "Retoma o bot para um lead pausado.",
            "parameters": {
                "type": "object",
                "properties": {"nome": {"type": "string"}, "telefone": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "proxima_agenda",
            "description": "Mostra próximas reuniões agendadas no Calendar.",
            "parameters": {
                "type": "object",
                "properties": {"periodo": {"type": "string", "enum": ["hoje", "semana"]}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "responder_texto",
            "description": "Quando nenhuma ferramenta se encaixa, responde diretamente ao corretor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mensagem": {"type": "string"},
                },
                "required": ["mensagem"],
            },
        },
    },
]


class LLMRouter:
    def __init__(self, model: str = OPENAI_MODEL) -> None:
        self.model = model
        self._client = None
        self._api_key = os.getenv("OPENAI_API_KEY", "").strip()

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def route(self, message: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not self.available:
            return {"tool": "responder_texto", "args": {"mensagem": "Não entendi. Reformule, por favor."}, "raw": None}

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for entry in (history or [])[-10:]:
            role = entry.get("role", "user")
            if role == "bot":
                role = "assistant"
            content = entry.get("content", "")
            if content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
            )
        except Exception as exc:
            return {
                "tool": "responder_texto",
                "args": {"mensagem": f"Falha ao consultar o LLM: {exc}"},
                "raw": None,
            }

        choice = response.choices[0].message
        tool_calls = choice.tool_calls or []
        if tool_calls:
            call = tool_calls[0]
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            return {"tool": call.function.name, "args": args, "raw": choice.content or ""}

        return {
            "tool": "responder_texto",
            "args": {"mensagem": choice.content or "Não entendi."},
            "raw": choice.content or "",
        }

    def route_vision(self, text: str, image_b64: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not self.available:
            return {"tool": "responder_texto", "args": {"mensagem": "Não entendi. Reformule, por favor."}, "raw": None}

        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for entry in (history or [])[-10:]:
            role = entry.get("role", "user")
            if role == "bot":
                role = "assistant"
            content = entry.get("content", "")
            if content:
                messages.append({"role": role, "content": content})

        content_parts: list[dict[str, Any]] = [
            {"type": "text", "text": text or "O usuário enviou esta imagem."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]
        messages.append({"role": "user", "content": content_parts})

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
            )
        except Exception as exc:
            return {
                "tool": "responder_texto",
                "args": {"mensagem": f"Falha ao consultar o LLM: {exc}"},
                "raw": None,
            }

        choice = response.choices[0].message
        tool_calls = choice.tool_calls or []
        if tool_calls:
            call = tool_calls[0]
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            return {"tool": call.function.name, "args": args, "raw": choice.content or ""}

        return {
            "tool": "responder_texto",
            "args": {"mensagem": choice.content or "Não consegui ler a imagem."},
            "raw": choice.content or "",
        }
