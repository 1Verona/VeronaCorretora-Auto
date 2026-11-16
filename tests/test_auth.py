import os
import subprocess
import sys

from conftest import ROOT, login


def test_login_ok(client):
    tok = login(client)
    r = client.get("/outreach/config", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200


def test_login_errado_401_json(client):
    r = client.post("/login", json={"username": "admin", "password": "errada"})
    assert r.status_code == 401
    assert r.get_json()["ok"] is False


def test_login_payload_estranho_nao_da_500(client):
    for body in ([1, 2], {"username": 5, "password": 7}, None):
        r = client.post("/login", json=body)
        assert r.status_code == 401
    r = client.post("/login", data="lixo", content_type="text/plain")
    assert r.status_code == 401


def test_api_sem_token_401(client):
    assert client.get("/outreach/config").status_code == 401
    assert client.get("/outreach/config", headers={"Authorization": "Bearer abc"}).status_code == 401


def test_health_publico(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.get_json()["db"] is True


def test_token_expira(app_module, monkeypatch):
    import time as _t
    tok = app_module._tokens.issue("admin")
    assert app_module._tokens.verify(tok) == "admin"
    real = _t.time
    monkeypatch.setattr(_t, "time", lambda: real() + app_module._tokens.ttl + 10)
    assert app_module._tokens.verify(tok) is None


def test_rate_limit_login(client, app_module):
    for _ in range(5):
        client.post("/login", json={"username": "admin", "password": "x"})
    r = client.post("/login", json={"username": "admin", "password": "senha-de-teste-forte"})
    assert r.status_code == 429


def test_cors_restrito(client):
    r = client.get("/health", headers={"Origin": "https://malicioso.com"})
    assert "Access-Control-Allow-Origin" not in r.headers
    r = client.get("/health", headers={"Origin": "https://painel.exemplo.com"})
    assert r.headers["Access-Control-Allow-Origin"] == "https://painel.exemplo.com"


def _boot(env_overrides):
    env = {**os.environ, **env_overrides}
    code = "import app"
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


def test_recusa_subir_sem_senha():
    r = _boot({"ADMIN_PASSWORD": ""})
    assert r.returncode != 0 and "Recusando subir" in (r.stderr + r.stdout)


def test_recusa_subir_com_secret_padrao():
    r = _boot({"APP_SECRET": "verona-dev-secret"})
    assert r.returncode != 0 and "APP_SECRET" in (r.stderr + r.stdout)
