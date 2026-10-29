import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Ambiente de teste — nada real
os.environ.update({
    "ADMIN_USER": "admin",
    "ADMIN_PASSWORD": "senha-de-teste-forte",
    "APP_SECRET": "x" * 48,
    "EVOLUTION_WEBHOOK_TOKEN": "tok-webhook-teste",
    "EVOLUTION_API_URL": "", "EVOLUTION_API_KEY": "", "EVOLUTION_INSTANCE": "",
    "TELEGRAM_BOT_TOKEN": "",
    "OPENAI_API_KEY": "sk-test",
    "CORS_ORIGINS": "https://painel.exemplo.com",
    "DB_SKIP_LEGACY_IMPORT": "1",
    "LOG_FORMAT": "plain",
})


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    import storage
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    storage.reset_for_tests()
    yield
    storage.reset_for_tests()


@pytest.fixture
def app_module():
    import app as app_mod
    app_mod._login_limiter.reset("127.0.0.1")
    return app_mod


@pytest.fixture
def client(app_module):
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def login(client):
    r = client.post("/login", json={"username": "admin", "password": "senha-de-teste-forte"})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["token"]
