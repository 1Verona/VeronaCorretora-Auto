"""Autenticação, rate limit, CORS e mascaramento de dados pessoais."""
from __future__ import annotations

import hmac
import os
import re
import threading
import time
from collections import defaultdict, deque

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

INSECURE_SECRETS = {"", "verona-dev-secret", "changeme", "secret", "troque-isto"}


class InsecureConfigError(RuntimeError):
    pass


def load_auth_settings() -> dict:
    """Lê e valida ADMIN_USER/ADMIN_PASSWORD/APP_SECRET. Recusa subir sem eles."""
    user = os.getenv("ADMIN_USER", "Admin").strip() or "Admin"
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    secret = os.getenv("APP_SECRET", "").strip()
    problems = []
    if not password:
        problems.append("ADMIN_PASSWORD não definido")
    elif len(password) < 10:
        problems.append("ADMIN_PASSWORD precisa ter pelo menos 10 caracteres")
    if secret in INSECURE_SECRETS or len(secret) < 32:
        problems.append("APP_SECRET ausente/fraco (use 32+ caracteres aleatórios: python -c \"import secrets;print(secrets.token_urlsafe(48))\")")
    if problems:
        raise InsecureConfigError("; ".join(problems))
    ttl_h = float(os.getenv("TOKEN_TTL_HOURS", "12") or 12)
    return {"user": user, "password": password, "secret": secret, "ttl": int(ttl_h * 3600)}


class TokenService:
    def __init__(self, secret: str, ttl_seconds: int) -> None:
        self._s = URLSafeTimedSerializer(secret, salt="verona-admin-login")
        self.ttl = ttl_seconds

    def issue(self, user: str) -> str:
        return self._s.dumps({"u": user})

    def verify(self, token: str, now: float | None = None) -> str | None:
        try:
            # itsdangerous usa time.time(); `now` só existe para testes via monkeypatch
            data = self._s.loads(token, max_age=self.ttl)
            return data.get("u")
        except (SignatureExpired, BadSignature, Exception):
            return None


def constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


class RateLimiter:
    """Janela deslizante em memória: max_attempts por window segundos por chave."""

    def __init__(self, max_attempts: int = 5, window: int = 300) -> None:
        self.max = max_attempts
        self.window = window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Registra uma tentativa; retorna False se estourou o limite."""
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(self._hits) > 10000:  # poda IPs antigos (memória não cresce sem limite)
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                    self._hits.pop(k, None)
                q = self._hits[key]
            if len(q) >= self.max:
                return False
            q.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


def cors_origins() -> set[str]:
    raw = os.getenv("CORS_ORIGINS", "").strip()
    return {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}


_DIGITS = re.compile(r"\d")


def mask_phone(phone: str) -> str:
    p = str(phone or "")
    if len(p) <= 4:
        return "***"
    return p[:4] + "*" * (len(p) - 6) + p[-2:]


def mask_text(text: str) -> str:
    t = str(text or "")
    return f"<{len(t)} caracteres>"
