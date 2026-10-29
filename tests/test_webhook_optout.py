import logging

import pytest

import storage


def _payload(phone="5511987654321", text="oi"):
    return {"data": {"key": {"remoteJid": f"{phone}@s.whatsapp.net", "fromMe": False}, "message": {"conversation": text}}}


@pytest.fixture
def engine(monkeypatch):
    import conversation_engine as ce
    sent = []
    monkeypatch.setattr(ce._evolution, "send_text", lambda phone, text: sent.append((phone, text)))
    monkeypatch.setattr(ce._sheets, "find_lead_by_phone", lambda phone: None)
    monkeypatch.setattr(ce._sheets, "mark_not_interested", lambda *a, **k: None)
    monkeypatch.setattr(ce._sheets, "update_stage", lambda *a, **k: None)
    monkeypatch.setattr(ce, "load_agent_config", lambda: {"test_mode": False, "history_limit": 20})
    calls = []

    def fake_llm(conv, text):
        calls.append(text)
        return {"tool": "responder", "args": {"mensagem": "Olá! Posso ajudar?", "novo_estagio": "ENGAGED"}}

    monkeypatch.setattr(ce, "_llm_decide", fake_llm)
    ce.sent, ce.llm_calls = sent, calls
    return ce


def test_webhook_sem_token(client):
    assert client.post("/webhook/evolution", json=_payload()).status_code == 401
    assert client.post("/webhook/evolution?token=errado", json=_payload()).status_code == 401


def test_webhook_sem_token_configurado(client, monkeypatch):
    monkeypatch.setenv("EVOLUTION_WEBHOOK_TOKEN", "")
    assert client.post("/webhook/evolution", json=_payload()).status_code == 503


def test_webhook_com_token_e_sem_vazar_log(client, engine, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.post("/webhook/evolution", json=_payload(text="mensagem secreta do lead"),
                    headers={"Authorization": "Bearer tok-webhook-teste"})
    assert r.status_code == 200
    assert engine.llm_calls == ["mensagem secreta do lead"]
    assert "mensagem secreta" not in caplog.text
    assert "5511987654321" not in caplog.text


@pytest.mark.parametrize("txt", ["SAIR", "parar", "Não quero", "não tenho interesse obrigado", "me tira da lista por favor"])
def test_opt_out_bloqueia(engine, txt):
    r = engine.handle_inbound_payload(_payload(text=txt))
    assert r["action"] == "opt_out"
    assert storage.is_blocked("5511987654321")
    assert storage.is_blocked("551187654321")  # variante sem o 9
    assert engine.llm_calls == []
    # depois de bloqueado: nunca mais responde
    engine.sent.clear()
    r2 = engine.handle_inbound_payload(_payload(text="oi de novo"))
    assert r2 == {"ignored": True, "reason": "blocked"}
    assert engine.sent == [] and engine.llm_calls == []


@pytest.mark.parametrize("txt", ["posso sair às 14h amanhã?", "quero saber mais"])
def test_nao_e_opt_out(txt):
    import optout
    assert optout.is_opt_out(txt) is False


def test_bloqueio_persiste(tmp_path):
    storage.block("5511999990000")
    storage.reset_for_tests()
    assert storage.is_blocked("5511999990000")
