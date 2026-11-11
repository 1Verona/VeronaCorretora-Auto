#!/usr/bin/env python3
"""Simula um dia de uso do VeronaCorretora-Auto, ponta a ponta, SEM rede.

Tudo é FICTÍCIO: nomes, telefones, CNPJs e e-mails. Evolution, OpenAI, Google
Sheets, Google Calendar, BigQuery, Overpass (OSM) e Telegram são fakes em memória.
Nenhuma mensagem real é enviada; nenhuma API paga é chamada.

Usa o código real: LeadGenWorker.run_now, OutreachWorker._loop (o mesmo laço do
servidor, com relógio simulado), o webhook Flask /webhook/evolution, o
conversation_engine, SheetManager, storage (SQLite temporário) e as rotas do painel.

Uso:  python sim/simular_dia.py [--data 2026-11-11] [--seed 7] [--json saida.json]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TMP = Path(tempfile.mkdtemp(prefix="vc_sim_"))
os.environ.update({
    "ADMIN_USER": "admin", "ADMIN_PASSWORD": "senha-FICTICIA-sim-123", "APP_SECRET": "s" * 48,
    "EVOLUTION_WEBHOOK_TOKEN": "tok-FICTICIO", "EVOLUTION_API_URL": "", "EVOLUTION_API_KEY": "",
    "EVOLUTION_INSTANCE": "", "TELEGRAM_BOT_TOKEN": "", "OPENAI_API_KEY": "sk-FICTICIO",
    "CORS_ORIGINS": "https://painel.ficticio.invalid", "DB_PATH": str(TMP / "sim.db"),
    "DB_SKIP_LEGACY_IMPORT": "1", "LOG_FORMAT": "plain", "LOG_LEVEL": "ERROR", "TZ": "America/Sao_Paulo",
})

import pytz  # noqa: E402

TZ = pytz.timezone("America/Sao_Paulo")


# ----------------------------------------------------------------- relógio
class Clock:
    t = 0.0


class SimDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(Clock.t, tz) if tz else datetime.fromtimestamp(Clock.t, TZ).replace(tzinfo=None)


time_shim = SimpleNamespace(time=lambda: Clock.t, sleep=lambda s: None)


def hhmm(ts: float | None = None) -> str:
    return datetime.fromtimestamp(Clock.t if ts is None else ts, TZ).strftime("%H:%M")


# ----------------------------------------------------------------- módulos reais
import agent_config  # noqa: E402
import calendar_manager  # noqa: E402
import cnpj_leads  # noqa: E402
import conversation_engine as ce  # noqa: E402
import lead_gen_worker as lg  # noqa: E402
import osm_leads  # noqa: E402
import outreach_worker as ow  # noqa: E402
import sheet_manager as sm  # noqa: E402
import sheets_config  # noqa: E402
import storage  # noqa: E402
from evolution_client import EvolutionError, normalize_phone  # noqa: E402
from fake_google import FakeCalendarService, FakeSheetsService  # noqa: E402

for mod, name in ((agent_config, "agent.json"), (ow, "outreach.json"), (lg, "leadgen.json"), (sheets_config, "sheets.json")):
    mod.CONFIG_PATH = TMP / name
for mod in (ow, ce, sm):
    mod.datetime = SimDT
ow.time = time_shim
ce.time = time_shim

import app as app_mod  # noqa: E402  (depois do ambiente pronto)

SHEETS = FakeSheetsService({})
CAL = FakeCalendarService()
calendar_manager.CalendarManager.service = property(lambda self: CAL)
ce._sheets._service = SHEETS
osm_leads.build_sheets_service = lambda *_a, **_k: SHEETS
cnpj_leads.build_sheets_service = lambda *_a, **_k: SHEETS


# ----------------------------------------------------------------- Telegram fake
class FakeTelegram:
    def __init__(self):
        self.msgs: list[tuple[str, str]] = []

    def __call__(self, text: str) -> None:
        self.msgs.append((hhmm(), text))


TG = FakeTelegram()
app_mod._notify_broker = TG


# ----------------------------------------------------------------- Evolution fake
class FakeEvolution:
    configured = True

    def __init__(self):
        self.sent: list[dict] = []
        self.fail_next = 0
        self.attempts = 0
        self.failures = 0

    def send_text(self, phone, text):
        self.attempts += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            self.failures += 1
            raise EvolutionError(503, "Evolution FICTÍCIA indisponível (simulado)")
        self.sent.append({"t": Clock.t, "phone": normalize_phone(phone), "text": text})
        return {"key": {"id": f"OUT{len(self.sent)}"}}


EVO = FakeEvolution()
ce._evolution = EVO


# ----------------------------------------------------------------- OpenAI fake (regras determinísticas)
class FakeOpenAI:
    def __init__(self):
        self.calls = 0
        self.outage = False

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, model, messages, tools, tool_choice, temperature, timeout):
        self.calls += 1
        if self.outage:
            raise TimeoutError("OpenAI FICTÍCIA: timeout simulado")
        sysmsg = messages[0]["content"]
        stage = re.search(r"Estágio atual: (\w+)", sysmsg).group(1)
        collected = json.loads(re.search(r"Dados já coletados: (\{.*?\})\n", sysmsg).group(1))
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        tool, args = self.decide(stage, collected, user)
        call = SimpleNamespace(function=SimpleNamespace(name=tool, arguments=json.dumps(args, ensure_ascii=False)))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call], content=None))])

    @staticmethod
    def _when(text: str) -> str | None:
        m = re.search(r"(hoje|amanh[ãa]).*?(\d{1,2})\s*h", text.lower())
        if not m:
            return None
        base = datetime.fromtimestamp(Clock.t, TZ).date() + timedelta(days=0 if m.group(1) == "hoje" else 1)
        return f"{base.isoformat()}T{int(m.group(2)):02d}:00"

    def decide(self, stage, collected, text):
        low = text.lower()
        email = re.search(r"[\w.+-]+@[\w.-]+", text)
        when = self._when(text)
        if "já tenho" in low or "ja tenho" in low:
            return "marcar_sem_interesse", {"motivo": "já possui seguro", "mensagem_despedida": "Entendo! Obrigada pelo retorno e boa semana."}
        if "quanto custa" in low or "cobertura" in low:
            return "pedir_humano", {"motivo": "pergunta de preço/cobertura", "mensagem_lead": "Ótima pergunta! Vou pedir para um consultor te responder com os valores."}
        if email and when and ("sou" in low or "nome" in low):
            nome = re.search(r"sou (?:a |o )?(.+?),", text, re.I)
            return "agendar", {"nome_completo": nome.group(1) if nome else "", "data_hora_iso": when, "email": email.group(0), "mensagem_confirmacao": "Combinado!"}
        if when and collected.get("email") and collected.get("nome_completo"):
            return "agendar", {"nome_completo": collected["nome_completo"], "data_hora_iso": when, "email": collected["email"], "mensagem_confirmacao": "Combinado!"}
        if stage == "COLLECTING_NAME":
            return "coletar_dado", {"campo": "nome_completo", "valor": text.strip(), "mensagem": "Obrigada! Qual o melhor dia e horário pra conversarmos 20 min?", "novo_estagio": "COLLECTING_DATETIME"}
        if stage == "COLLECTING_DATETIME" and when:
            return "coletar_dado", {"campo": "data_hora", "valor": when, "mensagem": "Perfeito. E qual e-mail para eu enviar o convite?", "novo_estagio": "COLLECTING_EMAIL"}
        if stage == "COLLECTING_EMAIL" and email:
            return "agendar", {"nome_completo": collected.get("nome_completo", ""), "data_hora_iso": collected.get("data_hora", ""), "email": email.group(0), "mensagem_confirmacao": "Reunião marcada! Vai chegar o convite no seu e-mail."}
        if any(k in low for k in ("quero saber", "tenho interesse", "me explica", "pode explicar")):
            return "responder", {"mensagem": "Que bom! Pra agendar uma conversa rápida com nosso consultor, qual seu nome completo?", "novo_estagio": "COLLECTING_NAME"}
        if stage == "COLLECTING_DATETIME":
            return "responder", {"mensagem": "Qual dia e horário ficam bons pra você?", "novo_estagio": "COLLECTING_DATETIME"}
        return "responder", {"mensagem": "Oi! Sou a Ana, da Verona Corretora. Posso te explicar em 2 minutos como funciona o seguro para advogados?", "novo_estagio": "ENGAGED"}


LLM = FakeOpenAI()
ce._openai_client = LLM

# ----------------------------------------------------------------- BigQuery / Overpass fakes
BQ_CALLS = Counter()


def fict_phone(i: int) -> str:
    return f"(48) 90000-{i:04d}"  # FICTÍCIO


def osm_elements():
    els = []
    for i in range(1, 19):
        els.append({"tags": {"name": f"Advocacia FICTÍCIA {i:02d}", "phone": fict_phone(i), "addr:city": "Florianópolis (FICTÍCIO)"}})
    els.append({"tags": {"name": "Escritório FICTÍCIO sem telefone"}})                  # descartado: sem telefone
    els.append({"tags": {"name": "Advocacia FICTÍCIA 01", "phone": fict_phone(1)}})       # duplicado no próprio OSM
    els.append({"tags": {"name": "Advocacia FICTÍCIA Dupla", "phone": fict_phone(500)}})  # também está no CNPJ
    return els


class FakeBQRow(SimpleNamespace):
    pass


class FakeBQ:
    def __init__(self, n=24):
        self.n = n

    def query(self, sql, job_config=None):
        kind = "snapshot" if "MAX(data)" in sql else "consulta_principal"
        BQ_CALLS[kind] += 1
        if kind == "snapshot":
            rows = [SimpleNamespace(latest=datetime(2026, 10, 15).date())]
        else:
            rows = []
            for i in range(self.n):
                phone = (f"48", f"9000{1000 + i:04d}") if i else ("48", "900000500")  # i=0 repete o OSM "Dupla"
                rows.append(FakeBQRow(
                    cnpj_basico=f"99{i:06d}", cnpj_ordem="0001", cnpj_dv="00", nome_fantasia="",
                    razao_social=f"SOCIEDADE DE ADVOGADOS FICTÍCIA {i:02d} LTDA", data_inicio_atividade=datetime(2023 - i % 10, 3, 1).date(),
                    cnae_fiscal_principal="6911701", ddd_1=phone[0], telefone_1=phone[1], ddd_2="", telefone_2="",
                    email=f"contato{i:02d}@ficticio.invalid", tipo_logradouro="RUA", logradouro="FICTÍCIA", numero=str(i),
                    bairro="CENTRO", cep="88000-000", municipio_nome="Florianópolis (FICTÍCIO)",
                ))
        return SimpleNamespace(result=lambda: iter(rows) if kind == "snapshot" else rows, total_bytes_processed=0)


osm_leads.fetch_osm_leads = lambda area, log_callback=None: osm_elements()
cnpj_leads.build_bq_client = lambda *_a, **_k: FakeBQ()


# ----------------------------------------------------------------- roteiro de respostas dos leads
# índice = ordem do 1º contato do dia (1 = primeiro lead que recebeu mensagem)
SCRIPT = {
    1: [(4, "Oi! Tenho interesse, quero saber mais"), (3, "Carlos FICTÍCIO da Silva"), (5, "amanhã às 15h"), (2, "carlos@ficticio.invalid")],
    2: [(10, "SAIR")],
    3: [(20, "Já tenho seguro com outra corretora, obrigado")],
    4: [(7, "Quanto custa? Tem cobertura pra invalidez?"), (30, "alô? alguém?")],
    6: [(15, "Sou a Dra. Beatriz FICTÍCIA, beatriz@ficticio.invalid, pode ser hoje às 8h"), (5, "então amanhã às 10h")],
    7: [(3, "não tenho interesse")],
    8: [(6, "me explica melhor como funciona?")],      # IA fora do ar nesse momento
    9: [(8, "Tenho interesse sim", "dup")],             # Evolution reenvia o mesmo webhook
    10: [(45, "Bom dia! Quem é?")],
}
OUTAGE_LEAD = 8


class Sim:
    def __init__(self, day: str, seed: int):
        random.seed(seed)
        self.day = datetime.strptime(day, "%Y-%m-%d").date()
        self.start = TZ.localize(datetime.combine(self.day, datetime.min.time()).replace(hour=7)).timestamp()
        self.end = TZ.localize(datetime.combine(self.day, datetime.min.time()).replace(hour=19)).timestamp()
        self.events: list[tuple[float, int, str, tuple]] = []
        self.seq = 0
        self.timeline: list[tuple[str, str]] = []
        self.first_contact_order: dict[str, int] = {}
        self.web = app_mod.app.test_client()
        self.token = None
        self.webhook_codes = Counter()
        self.sheet_calls_by_phase: dict[str, Counter] = {}

    # ---- utilidades
    def log(self, txt: str):
        self.timeline.append((hhmm(), txt))

    def at(self, ts: float, kind: str, *args):
        self.seq += 1
        self.events.append((ts, self.seq, kind, args))
        self.events.sort()

    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def run_due_events(self):
        while self.events and self.events[0][0] <= Clock.t:
            ts, _, kind, args = self.events.pop(0)
            saved = Clock.t
            Clock.t = ts
            getattr(self, f"ev_{kind}")(*args)
            Clock.t = max(saved, ts)

    # ---- eventos
    def ev_inbound(self, phone: str, text: str, mid: str, dup: bool = False, group: bool = False, from_me: bool = False):
        jid = f"{phone}@{'g.us' if group else 's.whatsapp.net'}"
        payload = {"event": "messages.upsert", "data": {"key": {"remoteJid": jid, "fromMe": from_me, "id": mid}, "message": {"conversation": text}}}
        idx = self.first_contact_order.get(phone)
        if idx == OUTAGE_LEAD:
            LLM.outage = True
        r = self.web.post("/webhook/evolution", json=payload, headers={"Authorization": "Bearer tok-FICTICIO"})
        LLM.outage = False
        self.webhook_codes[r.status_code] += 1
        res = (r.get_json() or {}).get("result") or {}
        label = res.get("action") or res.get("reason") or ("pausada" if res.get("paused") else "?")
        who = f"lead #{idx}" if idx else "número desconhecido"
        self.log(f"WhatsApp ← {who}: “{text}” → {label}")
        if dup:
            r2 = self.web.post("/webhook/evolution", json=payload, headers={"Authorization": "Bearer tok-FICTICIO"})
            res2 = (r2.get_json() or {}).get("result") or {}
            self.webhook_codes[r2.status_code] += 1
            self.log(f"Evolution reenviou o mesmo webhook (id {mid}) → {res2.get('reason')}")

    def ev_broker_block(self, phone: str):
        r = self.web.post("/blocklist", json={"phone": phone, "reason": "pedido por telefone (FICTÍCIO)"}, headers=self.auth())
        self.log(f"Corretor bloqueia um número pelo painel → HTTP {r.status_code}")

    def ev_evolution_outage(self, n: int):
        EVO.fail_next = n
        self.log(f"[falha simulada] Evolution vai recusar os próximos {n} envios")

    def ev_broker_restart(self):
        r = self.web.post("/outreach/start", headers=self.auth())
        self.log(f"Corretor vê o alerta e religa o disparo no painel → HTTP {r.status_code} (zera erros)")

    def ev_sheets_glitch(self):
        orig = SHEETS.values

        class Glitch:
            def __init__(s):
                s.done = False

            def __call__(s):
                v = orig()
                if not s.done:
                    real = v.batchUpdate

                    def bu(*a, **k):
                        s.done = True
                        SHEETS.values = orig
                        sim.log("[falha simulada] escrita no Sheets recusou (503) logo após um envio")
                        raise RuntimeError("Sheets FICTÍCIO 503 (simulado)")
                    v.batchUpdate = bu
                    _ = real
                return v
        sim = self
        SHEETS.values = Glitch()
        self.log("[falha simulada] próxima escrita no Sheets vai falhar (503)")

    def ev_panel_check(self, label: str):
        st = self.web.get("/outreach/status", headers=self.auth()).get_json()
        conv = self.web.get("/outreach/conversations", headers=self.auth()).get_json()
        self.log(f"Painel ({label}): enviados hoje {st['sent_today']}/{st['effective_daily_limit']}, fila {st['queue_size']}, "
                 f"bloqueados {st['blocked_count']}, conversas {len(conv['conversations'])}, pausa: “{st['paused_reason'] or '-'}”")

    def ev_leadgen(self, label: str):
        before = Counter(SHEETS.calls)
        res = app_mod.lead_gen_worker.run_now()
        r = res["result"]
        self.log(f"Geração de leads ({label}): OSM +{r['osm'].get('added', 0)} (ignorados {r['osm'].get('skipped', 0)}), "
                 f"CNPJ +{r['cnpj'].get('added', 0)}" + (" — consulta CNPJ pulada: snapshot já processado" if r['cnpj'].get('skipped') else "")
                 + f" | chamadas Sheets: {(Counter(SHEETS.calls) - before)['total']}")
        added = sum(x.get("added", 0) for x in r.values() if isinstance(x, dict))
        if added:
            TG(f"🔎 Geração automática de leads: {added} novo(s) lead(s) adicionados à planilha.")

    # ---- execução
    def run(self) -> dict:
        Clock.t = self.start
        # histórico: 2 dias anteriores de envio (10 e 20) → hoje é o 3º degrau da rampa (30)
        for d, n in ((2, 10), (1, 20)):
            day = (self.day - timedelta(days=d)).isoformat()
            storage.conn().execute("INSERT INTO daily_counters(day, sent) VALUES (?,?)", (day, n))
        storage.conn().commit()
        ow.save_config({**ow.DEFAULT_CONFIG, "enabled": True, "min_delay": 60, "max_delay": 180})
        lg.save_config({**lg.DEFAULT_CONFIG})
        sheets_config.save_config({"spreadsheet_id": "PLANILHA_FICTICIA", "source_sheet": ""})

        r = self.web.post("/login", json={"username": "admin", "password": "senha-FICTICIA-sim-123"})
        self.token = r.get_json()["token"]
        self.log("Corretor faz login no painel")

        self.ev_leadgen("07:00, automática")
        self.at(self.start + 5 * 60, "leadgen", "07:05, “buscar agora” no painel")
        self.at(self.start + 60 * 60, "panel_check", "08:00")
        self.at(self.start + 90 * 60, "broker_block", "4890001005")   # lead CNPJ 05, ainda na fila
        self.at(self.start + 2 * 3600 + 20 * 60, "sheets_glitch")      # 09:20
        self.at(self.start + 2 * 3600 + 40 * 60, "evolution_outage", 3)  # 09:40
        self.at(self.start + 3 * 3600 + 30 * 60, "broker_restart")     # 10:30
        self.at(self.start + 3 * 3600 + 15 * 60, "inbound", "5548911110000", "Oi, vi o anúncio de vocês (FICTÍCIO)", "INX1")
        self.at(self.start + 3 * 3600 + 20 * 60, "inbound", "120363000000000000", "mensagem de grupo FICTÍCIA", "GRP1", False, True)
        self.at(self.start + 3 * 3600 + 21 * 60, "inbound", "5548900000001", "mensagem enviada pelo próprio celular", "ME1", False, False, True)
        self.at(self.start + 9 * 3600, "panel_check", "16:00")

        worker = ow.OutreachWorker(sheet_manager=ce._sheets, evolution=EVO, notify_broker=lambda m: app_mod._notify_broker(m),
                                   conversation_hook=app_mod._conversation_seed)
        app_mod.outreach_worker = worker
        worker._thread = SimpleNamespace(is_alive=lambda: True)  # o laço roda aqui mesmo, não em thread
        orig_send = worker._send_one

        def send_hook(config, force=False):
            before = len(EVO.sent)
            res = orig_send(config, force)
            if res.get("sent"):
                phone = EVO.sent[before]["phone"]
                n = len(self.first_contact_order) + 1
                self.first_contact_order[phone] = n
                lim = ow.effective_daily_limit(config)
                self.log(f"Disparo #{n} → {res['lead']['nome']} ({res['lead']['sheet']}) [{worker._sent_today}/{lim}]")
                t = Clock.t
                for step in SCRIPT.get(n, []):
                    t += step[0] * 60
                    self.at(t, "inbound", phone, step[1], f"IN{n}-{int(t)}", len(step) > 2)
            elif res.get("error"):
                self.log(f"Disparo falhou: {res['error'][:60]} (erros seguidos: {worker._consecutive_errors})")
            return res

        worker._send_one = send_hook

        def sim_sleep(seconds):
            target = Clock.t + seconds
            while self.events and self.events[0][0] <= target:
                Clock.t = max(Clock.t, self.events[0][0])
                self.run_due_events()
            Clock.t = target
            if Clock.t >= self.end:
                worker._stop.set()

        worker._sleep = sim_sleep
        paused_logged = set()
        orig_refresh = worker._refresh_daily_counter

        def refresh(today):
            orig_refresh(today)
            if worker._paused_reason and worker._paused_reason not in paused_logged:
                paused_logged.add(worker._paused_reason)
                self.log(f"Laço de disparo: {worker._paused_reason}")
        worker._refresh_daily_counter = refresh
        worker._loop()   # o MESMO laço do servidor, com relógio simulado
        self.run_due_events()

        # depois do expediente
        r = self.web.post("/outreach/dispatch-now", headers=self.auth())
        self.log(f"Corretor tenta “disparar agora” depois do limite → HTTP {r.status_code} ({(r.get_json() or {}).get('error')})")
        self.ev_panel_check("19:00")
        health = self.web.get("/health").get_json()
        return self.report(health)

    def report(self, health) -> dict:
        rows = {s: SHEETS.rows_as_dicts(s) for s in SHEETS.data}
        stages = Counter()
        for s, rs in rows.items():
            for r in rs:
                stages[(r.get("estagio_conversa") or "(pendente)")] += 1
        first_msgs = [m for m in EVO.sent if m["phone"] in self.first_contact_order and "responda SAIR" in m["text"]]
        per_phone = Counter(m["phone"] for m in first_msgs)
        times = sorted(m["t"] for m in first_msgs)
        gaps = [b - a for a, b in zip(times, times[1:])]
        in_window = all(9 <= datetime.fromtimestamp(t, TZ).hour < 18 for t in times)
        convs = storage.all_conversations()
        return {
            "dia": self.day.isoformat(),
            "timeline": self.timeline,
            "telegram": TG.msgs,
            "numeros": {
                "leads_na_planilha": sum(len(v) for v in rows.values()),
                "abas": {k: len(v) for k, v in rows.items()},
                "primeiros_contatos": len(first_msgs),
                "limite_efetivo_hoje": ow.effective_daily_limit(ow.load_config(), self.day),
                "telefones_com_mais_de_1_primeira_msg": sum(1 for c in per_phone.values() if c > 1),
                "todos_dentro_da_janela_9_18": in_window,
                "intervalo_min_s": round(min(gaps)) if gaps else None,
                "intervalo_max_s": round(max(gaps)) if gaps else None,
                "primeiro_envio": hhmm(times[0]) if times else None,
                "ultimo_envio": hhmm(times[-1]) if times else None,
                "tentativas_evolution": EVO.attempts,
                "falhas_evolution_simuladas": EVO.failures,
                "mensagens_whatsapp_total": len(EVO.sent),
                "respostas_do_bot": len(EVO.sent) - len(first_msgs),
                "chamadas_llm": LLM.calls,
                "eventos_calendar": len(CAL.created),
                "chamadas_sheets": dict(SHEETS.calls),
                "chamadas_bigquery": dict(BQ_CALLS),
                "alertas_telegram": len(TG.msgs),
                "bloqueados": storage.count_blocked(),
                "envios_sem_marca_na_planilha_no_fim": storage.conn().execute("SELECT COUNT(*) FROM sent_log WHERE sheet_marked=0").fetchone()[0],
                "conversas": len(convs),
                "estagios_planilha": dict(stages),
                "webhook_http": dict(self.webhook_codes),
                "health": health,
            },
            "conversas": {
                p: {"lead": self.first_contact_order.get(p), "nome": c.get("nome"), "stage": c.get("stage"),
                    "history": [(h["role"], h["content"]) for h in c.get("history", [])]}
                for p, c in convs.items()
            },
            "agenda": [{"resumo": e["summary"], "inicio": e["start"]["dateTime"], "link": e["hangoutLink"]} for e in CAL.created],
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="2026-11-11", help="dia simulado (dia útil)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    out = Sim(a.data, a.seed).run()
    if a.json:
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    n = out["numeros"]
    print("== linha do tempo ==")
    for t, x in out["timeline"]:
        print(f"{t}  {x}")
    print("\n== Telegram (fake) ==")
    for t, x in out["telegram"]:
        print(f"{t}  {x.splitlines()[0]}")
    print("\n== números ==")
    for k, v in n.items():
        print(f"{k}: {v}")
    print(f"\n(dados temporários em {TMP})")


if __name__ == "__main__":
    main()
