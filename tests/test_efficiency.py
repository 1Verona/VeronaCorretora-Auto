"""Testes das melhorias do branch `efficiency` (sem rede; tudo com fakes)."""
import threading
import time
from datetime import datetime, timedelta

import pytest

import storage
from fake_google import FakeSheetsService

HDR = ["Status", "Nome", "Telefone", "Email", "Notas", "Follow-up"]


def _sheet(n=5):
    rows = [HDR] + [["", f"Dr. FICTÍCIO {i}", f"(48) 90000-{i:04d}", "", "", ""] for i in range(n)]
    import sheet_manager as sm
    svc = FakeSheetsService({"Leads_FICTICIO": rows})
    m = sm.SheetManager()
    m._service = svc
    return m, svc


# ---------------- planilha ----------------
def test_mark_first_contact_escreve_certo_e_com_poucas_chamadas():
    m, svc = _sheet()
    m.get_leads_pending_first_contact("Leads_FICTICIO", limit=5)  # cria colunas, aquece cache
    svc.calls.clear()
    m.mark_first_contact("Leads_FICTICIO", 2, datetime(2026, 11, 10, 9, 5), "Olá FICTÍCIO", "5548900000000@s.whatsapp.net")
    assert svc.calls["total"] <= 2  # antes: 7
    row = svc.rows_as_dicts("Leads_FICTICIO")[0]
    assert row["primeiro_contato_em"] == "10/11/2026 09:05"
    assert row["estagio_conversa"] == "ENGAGED"
    assert row["evolution_jid"] == "5548900000000@s.whatsapp.net"
    assert row["status"].startswith("PRIMEIRO CONTATO ENVIADO")
    assert "Template usado: Olá FICTÍCIO" in row["notas"]
    # nota é acrescentada, não sobrescrita
    m.mark_not_interested("Leads_FICTICIO", 2, "teste")
    row = svc.rows_as_dicts("Leads_FICTICIO")[0]
    assert "Template usado" in row["notas"] and "teste" in row["notas"]
    assert row["estagio_conversa"] == "NOT_INTERESTED"


def test_update_stage_uma_chamada_com_cache_quente():
    m, svc = _sheet()
    m.get_leads_pending_first_contact("Leads_FICTICIO", limit=5)
    svc.calls.clear()
    m.update_stage("Leads_FICTICIO", 3, "COLLECTING_NAME")
    assert svc.calls["total"] == 1
    assert svc.rows_as_dicts("Leads_FICTICIO")[1]["estagio_conversa"] == "COLLECTING_NAME"


def test_cache_velho_nao_duplica_coluna(monkeypatch):
    m, svc = _sheet()
    m.ensure_columns("Leads_FICTICIO", ["Status"])  # cache sem colunas de outreach
    # alguém adicionou a coluna direto na planilha
    svc.data["Leads_FICTICIO"][0].append("Estagio_conversa")
    m.ensure_columns("Leads_FICTICIO", ["Estagio_conversa"])
    hdr = [h.lower() for h in svc.data["Leads_FICTICIO"][0]]
    assert hdr.count("estagio_conversa") == 1


def test_find_lead_por_telefone_usa_batchget():
    m, svc = _sheet(50)
    svc.data["Outra_FICTICIA"] = [HDR, ["", "Dra. FICTÍCIA", "(48) 98888-0000", "", "", ""]]
    svc.calls.clear()
    lead = m.find_lead_by_phone("5548988880000")
    assert lead and lead["sheet"] == "Outra_FICTICIA"
    assert svc.calls["values.batchGet"] == 1 and svc.calls["values.get"] == 0


# ---------------- disparo ----------------
class _Ev:
    configured = True

    def __init__(self):
        self.sent = []

    def send_text(self, phone, text):
        self.sent.append(phone)


class _SheetsFalha:
    """mark_first_contact falha: antes o mesmo lead recebia a 1ª msg de novo."""

    def __init__(self):
        self.leads = [{"nome": "Dr FICTÍCIO", "telefone": "5548900001111", "sheet": "L", "row": 2}]
        self.fail = True
        self.marked = []

    def get_leads_pending_first_contact(self, source, limit=1):
        return [l for l in self.leads if l["row"] not in self.marked][:limit]

    def mark_first_contact(self, sheet_name, row, **kw):
        if self.fail:
            raise RuntimeError("Sheets 503")
        self.marked.append(row)

    def mark_not_interested(self, *a, **k):
        pass


@pytest.fixture
def ow(monkeypatch):
    import outreach_worker as ow
    monkeypatch.setattr(ow, "load_sheets_config", lambda: {"source_sheet": ""})
    monkeypatch.setattr(ow, "load_agent_config", lambda: {"test_mode": False})
    return ow


def test_sem_reenvio_quando_planilha_falha(ow):
    sh, ev = _SheetsFalha(), _Ev()
    w = ow.OutreachWorker(sheet_manager=sh, evolution=ev)
    cfg = {**ow.DEFAULT_CONFIG}
    assert w._send_one(cfg)["sent"]
    r = w._send_one(cfg)
    assert r.get("empty") and len(ev.sent) == 1
    sh.fail = False  # planilha voltou: marca o lead na próxima rodada, sem reenviar
    w._send_one(cfg)
    assert sh.marked == [2] and len(ev.sent) == 1


def test_disparo_manual_respeita_limite(ow, monkeypatch):
    monkeypatch.setattr(ow, "load_config", lambda: {**ow.DEFAULT_CONFIG, "warmup_enabled": False, "daily_limit": 2})
    from test_outreach import FakeSheets, FakeEvolution
    w = ow.OutreachWorker(sheet_manager=FakeSheets(), evolution=FakeEvolution())
    assert w.dispatch_one_now()["sent"]
    assert w.dispatch_one_now()["sent"]
    r = w.dispatch_one_now()
    assert not r["sent"] and "limite" in r["error"]


def test_envios_concorrentes_nao_duplicam_lead(ow):
    from test_outreach import FakeSheets

    class SlowEv(_Ev):
        def send_text(self, phone, text):
            time.sleep(0.02)
            super().send_text(phone, text)

    sh, ev = FakeSheets(20), SlowEv()
    w = ow.OutreachWorker(sheet_manager=sh, evolution=ev)
    ts = [threading.Thread(target=w._send_one, args=({**ow.DEFAULT_CONFIG},)) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ev.sent) == len(set(ev.sent)) == 6


# ---------------- webhook / conversa ----------------
def _payload(phone="5548911112222", text="oi", mid=None):
    key = {"remoteJid": f"{phone}@s.whatsapp.net", "fromMe": False}
    if mid:
        key["id"] = mid
    return {"data": {"key": key, "message": {"conversation": text}}}


@pytest.fixture
def ce(monkeypatch):
    import conversation_engine as ce
    sent, stages = [], []
    monkeypatch.setattr(ce._evolution, "send_text", lambda phone, text: sent.append(text))
    monkeypatch.setattr(ce._sheets, "find_lead_by_phone", lambda phone: {"nome": "Dr FICTÍCIO", "sheet": "L", "row": 2})
    monkeypatch.setattr(ce._sheets, "update_stage", lambda s, r, st: stages.append(st))
    monkeypatch.setattr(ce._sheets, "mark_not_interested", lambda *a, **k: None)
    monkeypatch.setattr(ce, "load_agent_config", lambda: {"test_mode": False, "history_limit": 20})
    ce.sent, ce.stages = sent, stages
    return ce


def test_webhook_duplicado_ignorado(ce, monkeypatch):
    calls = []
    monkeypatch.setattr(ce, "_llm_decide", lambda c, t: calls.append(t) or {"tool": "responder", "args": {"mensagem": "Oi!", "novo_estagio": "ENGAGED"}})
    ce.handle_inbound_payload(_payload(mid="ABC1"))
    r = ce.handle_inbound_payload(_payload(mid="ABC1"))
    assert r == {"ignored": True, "reason": "duplicado"} and len(calls) == 1 and len(ce.sent) == 1


def test_falha_da_ia_nao_responde_e_avisa(ce, monkeypatch):
    def boom(c, t):
        raise TimeoutError("openai timeout")
    monkeypatch.setattr(ce, "_llm_decide", boom)
    notes = []
    r = ce.handle_inbound_payload(_payload(text="quero saber mais"), notify_broker=notes.append)
    assert r == {"action": "llm_error"} and ce.sent == []
    assert "IA indisponível" in notes[0]
    assert ce._get("5548911112222")["history"][-1]["content"] == "quero saber mais"


def test_pausa_durante_a_ia_nao_envia(ce, monkeypatch):
    def decide(conv, text):
        ce.set_paused("5548911112222", True)  # corretor pausa enquanto a IA pensa
        return {"tool": "responder", "args": {"mensagem": "Oi!", "novo_estagio": "ENGAGED"}}
    ce.register_outbound_first_contact({"telefone": "5548911112222", "nome": "Dr FICTÍCIO", "sheet": "L", "row": 2})
    monkeypatch.setattr(ce, "_llm_decide", decide)
    r = ce.handle_inbound_payload(_payload())
    assert r.get("paused") and ce.sent == []
    assert ce._get("5548911112222")["paused_by_broker"] is True


def test_estagio_invalido_e_sync_so_quando_muda(ce, monkeypatch):
    seq = iter([
        {"tool": "responder", "args": {"mensagem": "a", "novo_estagio": "QUALQUER_COISA"}},
        {"tool": "responder", "args": {"mensagem": "b", "novo_estagio": "ENGAGED"}},
        {"tool": "responder", "args": {"mensagem": "c", "novo_estagio": "COLLECTING_NAME"}},
    ])
    monkeypatch.setattr(ce, "_llm_decide", lambda c, t: next(seq))
    for i in range(3):
        ce.handle_inbound_payload(_payload(text=f"msg {i}"))
    assert ce._get("5548911112222")["stage"] == "COLLECTING_NAME"
    assert ce.stages == ["ENGAGED", "COLLECTING_NAME"]  # antes: 3 escritas


def test_agenda_recusa_horario_passado(ce, monkeypatch):
    ontem = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT10:00")
    monkeypatch.setattr(ce, "_llm_decide", lambda c, t: {"tool": "agendar", "args": {
        "nome_completo": "Dr FICTÍCIO", "data_hora_iso": ontem, "email": "ficticio@exemplo.invalid", "mensagem_confirmacao": "ok"}})
    r = ce.handle_inbound_payload(_payload(text="pode ser ontem"))
    assert r["reason"] == "past_datetime" and "já passou" in ce.sent[-1]


# ---------------- painel / segurança ----------------
def test_config_disparo_invalida(client):
    from conftest import login
    tok = login(client)
    h = {"Authorization": f"Bearer {tok}"}
    for bad in ({"daily_limit": 0}, {"hour_start": 18, "hour_end": 9}, {"min_delay": 1}, {"warmup_schedule": [10, -1]}):
        r = client.post("/outreach/config", json=bad, headers=h)
        assert r.status_code == 400, bad


def test_xff_forjado_nao_burla_rate_limit(client, app_module):
    app_module._login_limiter.reset("8.8.8.8")
    codes = []
    for i in range(7):
        r = client.post("/login", json={"username": "x", "password": "y"},
                        headers={"X-Forwarded-For": f"1.2.3.{i}"}, environ_base={"REMOTE_ADDR": "8.8.8.8"})
        codes.append(r.status_code)
    assert codes[-1] == 429


def test_env_sem_injecao_de_linha(client, app_module, monkeypatch, tmp_path):
    from conftest import login
    monkeypatch.setattr(app_module, "ENV_PATH", tmp_path / ".env")
    tok = login(client)
    r = client.post("/evolution/config", json={"evolution_instance": "x\nADMIN_PASSWORD=hack"},
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400 and not (tmp_path / ".env").exists()


def test_evolution_le_env_na_criacao(monkeypatch):
    from evolution_client import EvolutionClient
    monkeypatch.setenv("EVOLUTION_API_URL", "http://evolution.ficticio.invalid")
    monkeypatch.setenv("EVOLUTION_API_KEY", "k")
    monkeypatch.setenv("EVOLUTION_INSTANCE", "i")
    assert EvolutionClient().configured


def test_telegram_allowlist(monkeypatch, tmp_path):
    import telegram_bot as tb
    monkeypatch.setattr(tb, "CONTEXT_FILE", tmp_path / "ctx.json")
    bot = tb.TelegramBot(token="")
    bot.context_store = tb.ContextStore(tmp_path / "ctx.json")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111,222")
    assert bot.is_authorized(111) and not bot.is_authorized(999)
    assert bot.broker_chat_id() == 111
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS")
    assert bot.is_authorized(555)          # 1º chat vira dono
    assert not bot.is_authorized(666)      # estranho bloqueado
    assert bot.broker_chat_id() == 555


def test_leadgen_nao_fica_travado(monkeypatch, tmp_path):
    import lead_gen_worker as lg
    monkeypatch.setattr(lg, "CONFIG_PATH", tmp_path / "lg.json")
    w = lg.LeadGenWorker()
    monkeypatch.setattr(lg, "load_sheets_config", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        w.run_now()
    assert w.status()["running"] is False
