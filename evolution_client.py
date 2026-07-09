from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "").strip().rstrip("/")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "").strip()
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "").strip()

DIGITS_RE = re.compile(r"\D+")


def normalize_phone(raw: str, default_country: str = "55") -> str:
    if not raw:
        return ""
    digits = DIGITS_RE.sub("", str(raw))
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if not digits.startswith(default_country):
        if len(digits) in (10, 11):
            digits = default_country + digits
    return digits


def phone_variants(raw: str) -> set[str]:
    """Retorna variantes do número (com e sem o 9 do celular BR) para matching robusto."""
    base = normalize_phone(raw)
    if not base:
        return set()
    variants = {base}
    # BR: celular tem 11 dígitos no nacional (DDD + 9 + 8 dígitos) => 13 com país
    if base.startswith("55") and len(base) == 13 and base[4] == "9":
        variants.add(base[:4] + base[5:])
    # Inverso: número sem 9 (12 dígitos) -> adicionar variante com 9
    if base.startswith("55") and len(base) == 12:
        variants.add(base[:4] + "9" + base[4:])
    return variants


def to_jid(phone: str) -> str:
    digits = normalize_phone(phone)
    if not digits:
        return ""
    return f"{digits}@s.whatsapp.net"


def jid_to_phone(jid: str) -> str:
    if not jid:
        return ""
    return jid.split("@", 1)[0]


@dataclass
class EvolutionError(Exception):
    status: int
    body: str

    def __str__(self) -> str:
        return f"Evolution {self.status}: {self.body[:200]}"


class EvolutionClient:
    def __init__(
        self,
        base_url: str = EVOLUTION_API_URL,
        api_key: str = EVOLUTION_API_KEY,
        instance: str = EVOLUTION_INSTANCE,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.instance = instance or ""
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.instance)

    @property
    def instance_path(self) -> str:
        return quote(self.instance, safe="")

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.api_key,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise EvolutionError(0, "Evolution não configurado (.env)")
        url = f"{self.base_url}{path}"
        resp = requests.post(url, headers=self._headers(), json=payload, timeout=self.timeout)
        if resp.status_code >= 400:
            raise EvolutionError(resp.status_code, resp.text)
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    def _get(self, path: str) -> dict[str, Any]:
        if not self.configured:
            raise EvolutionError(0, "Evolution não configurado (.env)")
        url = f"{self.base_url}{path}"
        resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            raise EvolutionError(resp.status_code, resp.text)
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    def send_text(self, phone: str, text: str) -> dict[str, Any]:
        number = normalize_phone(phone)
        if not number:
            raise EvolutionError(0, f"telefone inválido: {phone!r}")
        if not (text or "").strip():
            raise EvolutionError(0, "mensagem vazia — abortando envio para não mandar mensagem em branco")
        payload = {"number": number, "text": text}
        return self._post(f"/message/sendText/{self.instance_path}", payload)

    def set_webhook(self, url: str, events: list[str] | None = None) -> dict[str, Any]:
        events = events or ["MESSAGES_UPSERT"]
        payload = {
            "webhook": {
                "url": url,
                "enabled": True,
                "events": events,
                "webhookByEvents": False,
                "webhookBase64": False,
            }
        }
        return self._post(f"/webhook/set/{self.instance_path}", payload)

    def instance_status(self) -> dict[str, Any]:
        return self._get(f"/instance/connectionState/{self.instance_path}")

    def is_connected(self) -> bool:
        """Retorna True se a instância está conectada (state == open)."""
        try:
            data = self.instance_status()
            # A Evolution pode retornar {instance: {state: 'open'}} ou {state: 'open'}
            state = (
                (data.get("instance") or {}).get("state")
                or data.get("state")
                or ""
            )
            return state.lower() == "open"
        except Exception:
            return False

    def get_qr_code(self) -> dict[str, Any]:
        """Tenta obter o QR code da instância. Retorna {connected, qr_base64}."""
        if not self.configured:
            return {"connected": False, "qr_base64": None, "error": "Evolution não configurado"}
        try:
            # Primeiro verifica o estado atual
            if self.is_connected():
                return {"connected": True, "qr_base64": None}
            # Solicita o QR code
            data = self._get(f"/instance/connect/{self.instance_path}")
            # A Evolution retorna {base64: "data:image/png;base64,..."} ou {qrcode: {base64: ...}}
            qr = (
                data.get("base64")
                or (data.get("qrcode") or {}).get("base64")
                or (data.get("qrCode") or {}).get("base64")
                or ""
            )
            # Remove o prefixo data URI se presente
            if qr and "," in qr:
                qr = qr.split(",", 1)[1]
            return {"connected": False, "qr_base64": qr or None}
        except EvolutionError as exc:
            return {"connected": False, "qr_base64": None, "error": str(exc)}
