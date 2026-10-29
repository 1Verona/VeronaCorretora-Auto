# Changelog

## [hardening] — 2026-10-29

### Segurança
- Senha real removida do README.
- App recusa subir sem `ADMIN_PASSWORD` (10+) e `APP_SECRET` (32+, sem o antigo `verona-dev-secret`). Fim do "painel aberto" sem senha.
- Token de login assinado (itsdangerous) com expiração (`TOKEN_TTL_HOURS`, padrão 12h).
- Rate limit no `/login` (5 tentativas / 5 min por IP → 429).
- CORS restrito a `CORS_ORIGINS` (antes `*`); headers `nosniff` e `X-Frame-Options`.
- Webhook exige `EVOLUTION_WEBHOOK_TOKEN` (constant-time; 503 se não configurado) e não loga mais payload/telefone/texto.
- `GET /evolution/config` não devolve mais o token do webhook em texto puro.

### Correções
- `/login` com payload inválido (lista, números, texto) devolvia 500; agora 401 JSON. Painel trata 429 e respostas não-JSON.
- **Deadlock no webhook**: `conversation_engine._lock` não era reentrante e `_upsert` era chamado com o lock já preso — o processamento de mensagens travava. Agora `RLock`.

### Disparo / LGPD
- Opt-out: SAIR, PARAR, NÃO QUERO, etc. → bloqueio permanente (com variantes do número com/sem 9), confirmação única ao lead, aviso no Telegram, planilha marcada.
- Lista de bloqueio persistente (`GET/POST /blocklist`); leads bloqueados são pulados e marcados na planilha. "Sem interesse" da IA também bloqueia.
- Frase de saída na primeira mensagem (`opt_out_footer`, `OPT_OUT_FOOTER`).
- Rampa de aquecimento (`warmup_enabled`, `warmup_schedule` = 10/20/30/50).

### Robustez
- SQLite (`DB_PATH`) para conversas, bloqueios, contadores diários e erros consecutivos; importa `conversations.json` antigo uma vez.
- Escritas no Sheets serializadas por lock e com retry/backoff (`SHEETS_RETRIES`).
- Logging estruturado JSON (`LOG_LEVEL`, `LOG_FORMAT`). `/health` checa o banco (503 se falhar).
- Raspagem OAB: retry/timeout na abertura do site, aborta após N falhas seguidas e alerta no Telegram; falha de job também alerta.

### Repo / deploy
- Removidos do versionamento: `node_modules/`, `frontend/dist/`, `StartDev.app/`, `package.json` raiz (dependência "python" sem uso) e gitlinks quebrados `neon`/`oab_scraper`.
- Dockerfile multi-stage builda o painel; healthcheck; compose com volume `./data` e porta só em 127.0.0.1.
- README novo (fluxo mermaid, deploy, riscos), `.env.example` completo, testes pytest (28).
