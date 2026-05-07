from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

INTENT_KEYWORDS = {
    "fechou": [
        "fechou", "fechamos", "fechei", "contratou", "assinou", "comprou",
        "aceitou", "aceitou a proposta", "proposta aceita", "confirmou",
        "confirmou o serviço", "vendeu", "concretizou", "bateu o martelo",
        "fez o seguro", "contratou o plano", "topou", "deu o sim",
    ],
    "agendar_contato": [
        "contatar", "contato", "ligar", "retornar", "retorno", "falar",
        "entrar em contato", "daqui", "daqui a", "semana que vem",
        "mês que vem", "mes que vem", "depois", "mais tarde", "futuro",
        "agendar", "agenda", "lembrar", "lembrete", "cobrar", "follow up",
    ],
    "sem_interesse": [
        "sem interesse", "não quer", "nao quer", "não tem interesse",
        "recusou", "recusou a proposta", "dispensou", "mandou embora",
        "não precisa", "nao precisa", "já tem", "ja tem", "não respondeu",
        "nao respondeu", "ghost", "ignorou", "não atendeu", "nao atendeu",
    ],
    "em_negociacao": [
        "negociando", "em negociação", "em negociacao", "analisando",
        "analisando a proposta", "analisando proposta", "vendo",
        "vendo opções", "vendo opcoes", "comparando", "pensando",
        "vai pensar", "precisa ver", "vai ver com", "depende",
        "aguardando", "aguardando resposta", "ainda pensando",
    ],
    "agendar_visita": [
        "visita", "reunião", "reuniao", "presencial", "ir até", "ir ate",
        "encontrar", "encontro", "comparecer", "ir lá", "ir la",
    ],
    "enviar_proposta": [
        "enviar proposta", "manda proposta", "mandar proposta", "proposta",
        "orçamento", "orcamento", "cotação", "cotacao", "valor",
        "preço", "preco", "quanto custa", "tabela", "simulação",
        "simulacao", "passar valor", "passar preço", "passar preco",
    ],
}

MONTH_MAP = {
    "janeiro": 1, "jan": 1, "fevereiro": 2, "fev": 2, "março": 3,
    "mar": 3, "abril": 4, "abr": 4, "maio": 5, "junho": 6, "jun": 6,
    "julho": 7, "jul": 7, "agosto": 8, "ago": 8, "setembro": 9, "set": 9,
    "outubro": 10, "out": 10, "novembro": 11, "nov": 11, "dezembro": 12, "dez": 12,
}

RELATIVE_DATE_PATTERNS = [
    (r"(?:daqui[ a]+)(\d+)\s*(dias?|semanas?|m[eê]ses?|meses?|anos?)", "from_now"),
    (r"(\d+)\s*(dias?|semanas?|m[eê]ses?|meses?|anos?)\s*(?:daqui)?", "from_now"),
    (r"(?:na[ ao]?)\s*(\d{1,2})\s*(?:de\s+)?(jan(?:eiro)?|fev(?:ereiro)?|mar(?:[çc]o)?|abr(?:il)?|mai[o]?|jun(?:ho)?|jul(?:ho)?|ago(?:sto)?|set(?:embro)?|out(?:ubro)?|nov(?:embro)?|dez(?:embro)?)", "specific_date"),
    (r"(?:no[ ao]?)\s*(\d{1,2})\s*(?:\/|-)\s*(\d{1,2})", "day_month"),
    (r"semana\s+que\s+vem", "next_week"),
    (r"m[eê]s\s+que\s+vem|mes\s+que\s+vem", "next_month"),
    (r"amanh[aã]", "tomorrow"),
    (r"segunda\s+feira?|segunda", "next_monday"),
]


@dataclass
class ExtractedDate:
    date: datetime
    label: str = ""


@dataclass
class NLPResult:
    intent: str = "unknown"
    confidence: float = 0.0
    client_name: str = ""
    extracted_date: ExtractedDate | None = None
    notes: str = ""
    raw_message: str = ""


def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\sà-ú]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def detect_intent(message: str) -> tuple[str, float]:
    normalized = normalize_text(message)
    best_intent = "unknown"
    best_score = 0.0

    for intent, keywords in INTENT_KEYWORDS.items():
        score = 0.0
        for kw in keywords:
            kw_norm = normalize_text(kw)
            if kw_norm in normalized:
                kw_len = len(kw_norm.split())
                score += kw_len * 1.5
                if normalized.startswith(kw_norm) or normalized.endswith(kw_norm):
                    score += 1.0
        if score > best_score:
            best_score = score
            best_intent = intent

    if best_score > 0:
        confidence = min(best_score / 5.0, 1.0)
    else:
        confidence = 0.0

    return best_intent, confidence


def extract_relative_date(message: str) -> ExtractedDate | None:
    normalized = normalize_text(message)
    now = datetime.now()

    for pattern, date_type in RELATIVE_DATE_PATTERNS:
        match = re.search(pattern, normalized)
        if not match:
            continue

        if date_type == "from_now":
            value = int(match.group(1))
            unit = match.group(2).lower()
            if "dia" in unit:
                delta = timedelta(days=value)
            elif "semana" in unit:
                delta = timedelta(weeks=value)
            elif "mes" in unit or "mês" in unit:
                delta = timedelta(days=value * 30)
            elif "ano" in unit:
                delta = timedelta(days=value * 365)
            else:
                delta = timedelta(days=value)
            return ExtractedDate(date=now + delta, label=f"Contatar em {value} {unit}")

        elif date_type == "specific_date":
            day = int(match.group(1))
            month_str = match.group(2).lower()
            month = MONTH_MAP.get(month_str[:3], now.month)
            year = now.year if month >= now.month else now.year + 1
            try:
                return ExtractedDate(date=datetime(year, month, day), label=f"Contatar em {day}/{month}")
            except ValueError:
                continue

        elif date_type == "day_month":
            day = int(match.group(1))
            month = int(match.group(2))
            year = now.year if month >= now.month else now.year + 1
            try:
                return ExtractedDate(date=datetime(year, month, day), label=f"Contatar em {day}/{month}")
            except ValueError:
                continue

        elif date_type == "next_week":
            delta = timedelta(weeks=1)
            target = now + delta
            return ExtractedDate(date=target, label="Contatar semana que vem")

        elif date_type == "next_month":
            month = now.month + 1 if now.month < 12 else 1
            year = now.year if now.month < 12 else now.year + 1
            return ExtractedDate(date=datetime(year, month, 1), label="Contatar mês que vem")

        elif date_type == "tomorrow":
            return ExtractedDate(date=now + timedelta(days=1), label="Contatar amanhã")

        elif date_type == "next_monday":
            days_ahead = 7 - now.weekday() if now.weekday() >= 0 else 0
            if days_ahead == 0:
                days_ahead = 7
            return ExtractedDate(date=now + timedelta(days=days_ahead), label="Contatar segunda-feira")

    return None


def extract_notes(message: str, intent: str) -> str:
    cleaned = message.strip()
    if not cleaned:
        return ""
    if len(cleaned) < 4:
        return ""
    return cleaned


def parse_message(message: str) -> NLPResult:
    intent, confidence = detect_intent(message)
    extracted_date = extract_relative_date(message)
    notes = extract_notes(message, intent)

    return NLPResult(
        intent=intent,
        confidence=confidence,
        extracted_date=extracted_date,
        notes=notes,
        raw_message=message,
    )
