"""Detecção de pedidos de descadastro (opt-out) — LGPD / anti-banimento."""
from __future__ import annotations

import os
import re
import unicodedata

DEFAULT_KEYWORDS = [
    "sair", "parar", "pare", "stop", "cancelar", "descadastrar", "remover",
    "nao quero", "não quero", "nao tenho interesse", "não tenho interesse",
    "nao me mande", "não me mande", "nao mande mais", "não mande mais",
    "me tira da lista", "tira meu numero", "tira meu número", "bloquear", "spam",
]

OPT_OUT_FOOTER_DEFAULT = "Se não quiser receber mensagens, responda SAIR."


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def keywords() -> list[str]:
    extra = [k for k in os.getenv("OPT_OUT_KEYWORDS", "").split(",") if k.strip()]
    return [_norm(k) for k in DEFAULT_KEYWORDS + extra]


def is_opt_out(text: str) -> bool:
    t = _norm(text or "")
    if not t:
        return False
    words = t.split()
    for kw in keywords():
        if " " in kw:
            if kw in t:
                return True
        elif kw in words and (len(words) <= 2 or kw in ("descadastrar", "stop")):
            # palavra única só conta em mensagens curtas ("SAIR", "pode parar")
            return True
    return False


def footer() -> str:
    return os.getenv("OPT_OUT_FOOTER", OPT_OUT_FOOTER_DEFAULT).strip()


def ensure_footer(text: str) -> str:
    f = footer()
    if not f or f.lower() in text.lower():
        return text
    return f"{text}\n\n{f}"
