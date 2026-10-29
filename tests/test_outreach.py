from datetime import date

import pytest

import outreach_worker as ow
import storage
from evolution_client import EvolutionError


class FakeSheets:
    def __init__(self, n=200):
        self.leads = [{"nome": f"Dr Fulano {i}", "telefone": f"55119{i:08d}", "sheet": "Leads", "row": i + 2} for i in range(n)]
        self.marked, self.not_interested = [], []

    def get_leads_pending_first_contact(self, source, limit=1):
        done = {m for m in self.marked} | {r for r in self.not_interested}
        return [l for l in self.leads if l["row"] not in done][:limit]

    def mark_first_contact(self, sheet_name, row, **kw):
        self.marked.append(row)

    def mark_not_interested(self, sheet_name, row, reason=""):
        self.not_interested.append(row)


class FakeEvolution:
    configured = True

    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def send_text(self, phone, text):
        if self.fail:
            raise EvolutionError(500, "falha simulada")
        self.sent.append((phone, text))


@pytest.fixture(autouse=True)
def no_cfg(monkeypatch):
    monkeypatch.setattr(ow, "load_sheets_config", lambda: {"source_sheet": ""})
    monkeypatch.setattr(ow, "load_agent_config", lambda: {"test_mode": False})


def make(fail=False):
    notes = []
    w = ow.OutreachWorker(sheet_manager=FakeSheets(), evolution=FakeEvolution(fail), notify_broker=notes.append)
    return w, notes


def cfg(**kw):
    return {**ow.DEFAULT_CONFIG, **kw}


def test_primeira_mensagem_tem_frase_de_saida():
    w, _ = make()
    r = w._send_one(cfg())
    assert r["sent"]
    assert "responda SAIR" in w.evolution.sent[0][1]


def test_pula_bloqueados():
    w, _ = make()
    storage.block(w.sheet_manager.leads[0]["telefone"])
    w._send_one(cfg())
    assert w.evolution.sent[0][0] == w.sheet_manager.leads[1]["telefone"]
    assert 2 in w.sheet_manager.not_interested


def test_pausa_apos_3_erros_e_persiste():
    w, notes = make(fail=True)
    for _ in range(3):
        w._send_one(cfg())
    assert w._consecutive_errors == 3
    assert any("3 erros" in n for n in notes)
    w2, _ = make()
    assert w2._consecutive_errors == 3  # sobrevive a reinício


def test_contador_diario_persiste():
    w, _ = make()
    w._send_one(cfg(warmup_enabled=False))
    w._send_one(cfg(warmup_enabled=False))
    today = ow.datetime.now(ow.TZ).date()
    assert storage.get_sent(today.isoformat()) == 2
    w2, _ = make()
    w2._refresh_daily_counter(today)
    assert w2._sent_today == 2


def test_rampa_de_aquecimento():
    c = cfg(daily_limit=50, warmup_schedule=[10, 20, 30, 50])
    d = [date(2026, 11, i) for i in range(2, 8)]
    assert ow.effective_daily_limit(c, d[0]) == 10
    storage.incr_sent(d[0].isoformat())
    assert ow.effective_daily_limit(c, d[0]) == 10  # mesmo dia continua 10
    assert ow.effective_daily_limit(c, d[1]) == 20
    storage.incr_sent(d[1].isoformat()); storage.incr_sent(d[2].isoformat())
    assert ow.effective_daily_limit(c, d[3]) == 50
    storage.incr_sent(d[3].isoformat())
    assert ow.effective_daily_limit(c, d[4]) == 50
    assert ow.effective_daily_limit(cfg(daily_limit=40, warmup_enabled=False), d[0]) == 40
    assert ow.effective_daily_limit(cfg(daily_limit=5), d[5]) == 5


def test_limite_diario_notifica():
    w, notes = make()
    c = cfg(daily_limit=2, warmup_enabled=False)
    w._send_one(c); w._send_one(c)
    assert any("Limite diário" in n for n in notes)


def test_janela_horario():
    tz = ow.TZ
    seg_10h = tz.localize(ow.datetime(2026, 11, 2, 10))
    seg_20h = tz.localize(ow.datetime(2026, 11, 2, 20))
    sab_10h = tz.localize(ow.datetime(2026, 11, 7, 10))
    assert ow.within_window(seg_10h, cfg())
    assert not ow.within_window(seg_20h, cfg())
    assert not ow.within_window(sab_10h, cfg())
