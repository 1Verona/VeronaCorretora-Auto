# Changelog

## [efficiency] — 2026-11-10

Auditoria completa + otimizações de baixo risco. Regras de negócio (limite diário,
rampa 10/20/30/50, janela, intervalo, opt-out) iguais ou mais rígidas.

### Desempenho (medido com fake do Sheets que conta chamadas)
- Planilha: cache curto de cabeçalho (`SHEETS_HEADER_TTL`, 60s) e da lista de abas (`SHEETS_LIST_TTL`, 300s);
  campos + status + nota + follow-up numa única `batchUpdate`.
  `mark_first_contact` / `mark_meeting_scheduled` / `mark_not_interested`: 7 → 2 chamadas; `update_stage`: 3 → 1;
  leitura da fila (cache quente): 2 → 1.
- Leituras de todas as abas (busca por telefone, Telegram: cliente/follow-ups/estatísticas) numa só `values.batchGet`.
- Estágio da conversa só é gravado na planilha quando muda (antes: 1 escrita por mensagem do lead).
- Webhook: dedup por id da mensagem (Evolution reenvia em timeout) → sem IA/resposta em dobro.
- CNPJ/BigQuery: consulta principal não roda de novo se o snapshot da Receita é o mesmo (`cnpj_force` força).
- Evolution e Telegram→API com `requests.Session` (keep-alive). Config do agente com cache por mtime.
- SQLite: `synchronous=NORMAL` com WAL, índices novos, contagem de bloqueados sem carregar a lista.
- Painel: não faz polling com a aba do navegador oculta.

### Correções (bugs reais)
- **Disparo duplicado**: se a planilha falhava após o envio, o lead recebia a 1ª mensagem de novo; agora há
  registro local (`sent_log`) e a linha é marcada depois. Mesmo telefone em duas abas (OSM + CNPJ) recebe 1 vez (linha marcada `DUPLICADO`).
- **Corrida no limite**: laço + "disparar agora"/Telegram podiam pegar o mesmo lead e passar do limite; envio serializado e limite revalidado dentro do lock.
- **"Disparar agora" ignorava o limite diário/rampa**; agora respeita (janela continua livre para teste).
- **Pausa por erros ficava presa para sempre** (gravada no banco, sem como zerar): `POST /outreach/reset-errors` e "iniciar disparo" zeram.
- Linhas bloqueadas/sem interesse continuavam "pendentes": eram remarcadas a cada ciclo (nota crescendo) e podiam travar a fila.
- **Telegram quebrado desde o hardening**: o bot chamava a própria API sem token (401); agora usa token interno.
- Salvar Evolution pelo painel não tinha efeito até reiniciar (padrões congelados no import) e as respostas do webhook usavam o cliente antigo.
- Lock global do webhook segurava todos os leads durante a busca na planilha → lock por telefone.
- Pausa feita pelo corretor enquanto a IA respondia era desfeita / a IA respondia mesmo assim.
- IA fora do ar → 500 e reenvio da Evolution; agora guarda a mensagem e avisa no Telegram.
- Estágio inválido vindo da IA era gravado; agendamento aceitava horário passado e podia duplicar evento.
- Alertas do Telegram com `_`/`*` no texto do lead falhavam em silêncio (Markdown); agora reenvia em texto puro.
- Leadgen: exceção deixava "busca em andamento" para sempre; aba ignorava a UF configurada.
- Saudação "Olá Advocacia!" / "Olá SOCIEDADE!" para leads de escritório → "Prezado(a)"; nomes em CAIXA ALTA capitalizados.
- Horários de status/nota no fuso `TZ` (antes: fuso do servidor; no Docker, UTC).

### Segurança
- **Telegram sem controle de acesso**: qualquer pessoa que achasse o bot consultava leads e disparava; agora
  `TELEGRAM_ALLOWED_CHAT_IDS` (ou o dono já registrado). Alertas iam para "o 1º chat que falou com o bot".
- Telegram com TLS verificado (era `verify=False`).
- Rate limit do login burlável forjando `X-Forwarded-For`; agora só confia no header vindo do proxy.
- `POST /evolution/config` permitia injetar linhas no `.env` (ex.: trocar `ADMIN_PASSWORD`).
- Validação da config de disparo (janela, limite 1–1000, intervalo mínimo 10s, rampa positiva).
- Configs JSON com escrita atômica.

### Testes / ferramentas
- 28 → 55 testes (pytest, ~1,6 s). Fakes de Sheets/Calendar com contador (`tests/fake_google.py`).
- `sim/simular_dia.py`: simulação reproduzível de um dia inteiro com dados FICTÍCIOS e tudo mockado.
- pyflakes limpo; build do painel (Vite) ok.

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
