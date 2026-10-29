# Verona Corretora Auto — Painel de Controle

Captação de leads e automação de primeiro contato/atendimento para corretores de seguros OABPrev.

## Fluxo de ponta a ponta

```mermaid
flowchart LR
  subgraph Captação
    OAB[CNA OAB<br/>Playwright] --> S[(Google Sheets)]
    OSM[OpenStreetMap] --> S
    CNPJ[Base CNPJ] --> S
  end
  S -->|leads pendentes| W[OutreachWorker<br/>janela, limite, rampa,<br/>bloqueios]
  W -->|1ª mensagem + "responda SAIR"| EVO[Evolution API<br/>WhatsApp]
  EVO -->|webhook com token| WH[/webhook/evolution/]
  WH --> OPT{pediu para sair?}
  OPT -->|sim| BL[(SQLite: lista de bloqueio)]
  OPT -->|não| IA[IA OpenAI<br/>conversa]
  IA -->|agendar| CAL[Google Calendar]
  IA --> S
  CAL --> TG[Telegram do corretor]
  W -.alertas.-> TG
  P[Painel React<br/>login c/ token 12h] -->|API Flask| W
```

## Configuração

1. `cp .env.example .env` e preencha. **`ADMIN_PASSWORD` (10+ caracteres) e `APP_SECRET` (32+) são obrigatórios** — sem eles o app se recusa a subir.
2. `EVOLUTION_WEBHOOK_TOKEN` é obrigatório para o webhook funcionar; cadastre o mesmo token na Evolution.
3. Credenciais Google: `credentials.json` (service account) ou `GOOGLE_CREDENTIALS`.

## Rodar local

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && python -m playwright install chromium
python app.py                     # backend na 5050 (ou PORT)
cd frontend && npm ci && npm run dev   # painel em http://localhost:3000 (proxy p/ backend)
```

Testes: `pip install pytest && pytest -q`

## Deploy (resumo)

```bash
cp .env.example .env    # preencha
docker compose up -d --build     # builda o painel e o backend
curl -fsS http://127.0.0.1:5050/health
```

Coloque um proxy HTTPS na frente (ver `deploy/Caddyfile.snippet`, `deploy/DEPLOY.md` e `Hosting.md`). O estado fica em `./data/verona.db` — faça backup dessa pasta.

## Disparo: regras de proteção

| Regra | Padrão | Onde muda |
|---|---|---|
| Janela de horário | 9h–18h, só dias úteis | painel (`hour_start`, `hour_end`, `weekdays_only`) |
| Limite diário | 50 | painel (`daily_limit`) |
| Rampa de aquecimento | 10, 20, 30, 50 nos 4 primeiros dias com envio | painel (`warmup_enabled`, `warmup_schedule`) |
| Intervalo entre envios | 60–180 s aleatório | painel (`min_delay`, `max_delay`) |
| Pausa por erros | após 3 erros seguidos (persistente; reinício não zera) | painel (`max_consecutive_errors`) |
| Frase de saída na 1ª mensagem | ligada | painel (`opt_out_footer`) / `OPT_OUT_FOOTER` |
| Opt-out | SAIR, PARAR, NÃO QUERO, etc. → bloqueio permanente | `OPT_OUT_KEYWORDS` |

Leads marcados "sem interesse" pela IA também vão para a lista de bloqueio. `GET/POST /blocklist` lista (telefones mascarados) e adiciona bloqueios manuais.

## Segurança

- Login gera token assinado com validade (`TOKEN_TTL_HOURS`, padrão 12h); rate limit de 5 tentativas / 5 min por IP.
- CORS fechado por padrão; libere domínios em `CORS_ORIGINS`.
- Webhook exige `EVOLUTION_WEBHOOK_TOKEN` (comparação em tempo constante) e não registra texto nem telefone das mensagens.
- Logs em JSON (`LOG_LEVEL`, `LOG_FORMAT`). `/health` é público e não expõe dados.

## Riscos (leia antes de usar)

- **WhatsApp não oficial:** a Evolution API usa o WhatsApp Web. Mensagens frias em volume para listas raspadas são a principal causa de banimento do número. As proteções acima reduzem o risco, mas não eliminam. O caminho seguro é a WhatsApp Business Platform oficial com templates aprovados.
- **LGPD:** contatar profissionais a partir de dados públicos exige base legal (legítimo interesse documentado), transparência e opção de saída respeitada para sempre — o sistema registra e honra o opt-out, mas a responsabilidade jurídica é do corretor. Não guarde mais dados do que o necessário; o SQLite contém conversas: proteja o servidor e os backups.
- **Raspagem:** o CNA da OAB pode mudar o layout, exigir captcha ou bloquear o IP, e os termos de uso podem proibir coleta automatizada. O scraper tenta de novo, aborta após falhas seguidas e avisa no Telegram, mas pode parar de funcionar a qualquer momento.
