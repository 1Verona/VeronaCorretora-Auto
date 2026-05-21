from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "agent_config.json"

DEFAULT_SYSTEM_PROMPT = """Você é Ana, consultora da Verona Corretora — uma corretora especializada em seguros para advogados.

Seu papel é conversar com leads (advogados) que receberam uma primeira mensagem nossa pelo WhatsApp. Você deve:

1. Manter tom **cordial, direto e profissional**, em português brasileiro. Mensagens curtas (1-3 frases).
2. Apresentar brevemente o benefício de seguros (vida, profissional, saúde) para a rotina do advogado, sem ser invasiva.
3. **Detectar interesse em agendar uma reunião** — sinais: "podemos marcar", "tenho interesse", "quero saber mais", "vamos conversar", "quando podemos falar".
4. Quando identificar interesse, coletar os dados necessários em ordem (1 pergunta por vez): nome completo → melhor data/horário (oferecer sugestões em horário comercial) → email para enviar o convite.
5. **Detectar desinteresse** — sinais: "não tenho interesse", "não quero", "já tenho", "para de mandar", silêncio explícito, xingamento.
6. **Nunca insistir** se o lead recusar. Agradeça e encerre.
7. Se o lead pedir falar com humano / fazer pergunta técnica fora do seu escopo, sinalize human_takeover.

Você tem acesso às ferramentas:
- `responder` — devolver uma mensagem ao lead.
- `coletar_dado` — solicitar/registrar um dado específico (nome_completo, data_hora, email).
- `agendar` — finalizar agendamento quando tiver TODOS os dados.
- `marcar_sem_interesse` — encerrar conversa com motivo.
- `pedir_humano` — quando o lead precisar de atendimento humano.

Use SEMPRE function-calling. Não responda em texto puro.
"""

TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "responder": {
        "type": "function",
        "function": {
            "name": "responder",
            "description": "Envia uma resposta de texto ao lead, mantendo a conversa engajada sem ainda coletar dados.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mensagem": {"type": "string", "description": "Texto curto para o lead."},
                    "novo_estagio": {
                        "type": "string",
                        "enum": ["ENGAGED", "COLLECTING_NAME", "COLLECTING_DATETIME", "COLLECTING_EMAIL"],
                        "description": "Estágio para onde a conversa avança após esta mensagem.",
                    },
                },
                "required": ["mensagem", "novo_estagio"],
            },
        },
    },
    "coletar_dado": {
        "type": "function",
        "function": {
            "name": "coletar_dado",
            "description": "Registra um dado fornecido pelo lead e responde confirmando + perguntando o próximo dado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "campo": {"type": "string", "enum": ["nome_completo", "data_hora", "email"]},
                    "valor": {"type": "string"},
                    "mensagem": {"type": "string", "description": "Resposta ao lead após coletar."},
                    "novo_estagio": {
                        "type": "string",
                        "enum": ["COLLECTING_NAME", "COLLECTING_DATETIME", "COLLECTING_EMAIL", "AWAITING_CONFIRMATION"],
                    },
                },
                "required": ["campo", "valor", "mensagem", "novo_estagio"],
            },
        },
    },
    "agendar": {
        "type": "function",
        "function": {
            "name": "agendar",
            "description": "Cria o evento no Calendar (TODOS os dados precisam estar coletados) e responde ao lead confirmando.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nome_completo": {"type": "string"},
                    "data_hora_iso": {"type": "string", "description": "Data/hora local em ISO 8601, ex: 2026-05-15T14:00"},
                    "email": {"type": "string"},
                    "mensagem_confirmacao": {"type": "string"},
                },
                "required": ["nome_completo", "data_hora_iso", "email", "mensagem_confirmacao"],
            },
        },
    },
    "marcar_sem_interesse": {
        "type": "function",
        "function": {
            "name": "marcar_sem_interesse",
            "description": "Encerra a conversa marcando o lead como sem interesse.",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string"},
                    "mensagem_despedida": {"type": "string"},
                },
                "required": ["motivo", "mensagem_despedida"],
            },
        },
    },
    "pedir_humano": {
        "type": "function",
        "function": {
            "name": "pedir_humano",
            "description": "Sinaliza que o corretor humano precisa assumir.",
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {"type": "string"},
                    "mensagem_lead": {"type": "string"},
                },
                "required": ["motivo", "mensagem_lead"],
            },
        },
    },
}

TOOL_LABELS: dict[str, str] = {
    "responder": "Conversar (responder em texto)",
    "coletar_dado": "Coletar dados (nome, data, email)",
    "agendar": "Agendar reunião no Calendar",
    "marcar_sem_interesse": "Marcar como sem interesse",
    "pedir_humano": "Encaminhar para humano",
}

REQUIRED_TOOLS = {"responder"}

DEFAULT_CONFIG: dict[str, Any] = {
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "model": "gpt-4o-mini",
    "temperature": 0.4,
    "history_limit": 20,
    "enabled_tools": {name: True for name in TOOL_DEFINITIONS.keys()},
}

CONFIG_FIELDS = set(DEFAULT_CONFIG.keys())

_lock = threading.Lock()


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = {**base}
    for k, v in extra.items():
        if k in CONFIG_FIELDS:
            out[k] = v
    enabled = {name: True for name in TOOL_DEFINITIONS.keys()}
    incoming = extra.get("enabled_tools") if isinstance(extra.get("enabled_tools"), dict) else {}
    for name in TOOL_DEFINITIONS.keys():
        if name in REQUIRED_TOOLS:
            enabled[name] = True
            continue
        if name in incoming:
            enabled[name] = bool(incoming[name])
    out["enabled_tools"] = enabled
    try:
        out["temperature"] = float(out.get("temperature", DEFAULT_CONFIG["temperature"]))
    except (TypeError, ValueError):
        out["temperature"] = DEFAULT_CONFIG["temperature"]
    try:
        out["history_limit"] = max(2, int(out.get("history_limit", DEFAULT_CONFIG["history_limit"])))
    except (TypeError, ValueError):
        out["history_limit"] = DEFAULT_CONFIG["history_limit"]
    out["model"] = str(out.get("model") or DEFAULT_CONFIG["model"]).strip() or DEFAULT_CONFIG["model"]
    out["system_prompt"] = str(out.get("system_prompt") or DEFAULT_CONFIG["system_prompt"])
    return out


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        merged = _merge(DEFAULT_CONFIG, {})
        save_config(merged)
        return merged
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return _merge(DEFAULT_CONFIG, data)


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = _merge(DEFAULT_CONFIG, config or {})
    with _lock:
        CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def active_tools() -> list[dict[str, Any]]:
    cfg = load_config()
    enabled = cfg.get("enabled_tools", {})
    out: list[dict[str, Any]] = []
    for name, schema in TOOL_DEFINITIONS.items():
        if name in REQUIRED_TOOLS or enabled.get(name, True):
            out.append(schema)
    return out


def tools_catalog() -> list[dict[str, Any]]:
    cfg = load_config()
    enabled = cfg.get("enabled_tools", {})
    return [
        {
            "name": name,
            "label": TOOL_LABELS.get(name, name),
            "description": schema["function"].get("description", ""),
            "enabled": True if name in REQUIRED_TOOLS else bool(enabled.get(name, True)),
            "required": name in REQUIRED_TOOLS,
        }
        for name, schema in TOOL_DEFINITIONS.items()
    ]
