# Deploy na VM

Três caminhos. Escolha o que combina com o setup atual da sua VM.

---

## Caminho A — Easypanel (mesmo painel do Evolution)

1. No painel do Easypanel, clique em **Create → App → From Git**.
2. Repositório: aponte pra esse projeto (Git remote ou upload manual).
3. Build: **Dockerfile** (já existe na raiz).
4. Porta: `5050`.
5. **Environment variables** — copie os valores do seu `.env` local (NÃO comite o `.env`). Variáveis necessárias:
   ```
   OPENAI_API_KEY=<sua chave OpenAI>
   OPENAI_MODEL=gpt-4o-mini
   TELEGRAM_BOT_TOKEN=<token do bot>
   EVOLUTION_API_URL=<URL HTTPS da sua instância Evolution>
   EVOLUTION_API_KEY=<API key do Evolution>
   EVOLUTION_INSTANCE=<nome da instância>
   EVOLUTION_WEBHOOK_TOKEN=<gere com: openssl rand -hex 32>
   CALENDAR_ID=<ID do calendário Google compartilhado com a Service Account>
   BROKER_EMAIL_FOR_CALENDAR=<email do corretor>
   TZ=America/Sao_Paulo
   ```
6. **Mounts / Volumes** (pra preservar estado entre deploys):
   - `/app/credentials.json` → upload o arquivo
   - `/app/conversations.json` → persistent volume
   - `/app/outreach_config.json` → persistent volume
   - `/app/bot_contexts.json` → persistent volume
7. **Domain**: aponte `oabprev.goaether.xyz` pro container, porta 5050. HTTPS automático.
8. Deploy.

---

## Caminho B — SSH + systemd + Caddy

```bash
ssh verona@SEU_IP
sudo mkdir -p /opt/verona-auto
sudo chown verona:verona /opt/verona-auto
cd /opt/verona-auto

# Clonar (ou rsync) o projeto
git clone <URL_DO_REPO> .

# Setup Python
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium

# Configurar .env e credentials.json (copiar do seu Mac)
# scp .env credentials.json verona@SEU_IP:/opt/verona-auto/

# Instalar systemd unit
sudo cp deploy/verona-auto.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now verona-auto
sudo systemctl status verona-auto

# Configurar Caddy
sudo cat deploy/Caddyfile.snippet | sudo tee -a /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

---

## Caminho C — SSH + docker compose

```bash
ssh verona@SEU_IP
sudo mkdir -p /opt/verona-auto
cd /opt/verona-auto
git clone <URL_DO_REPO> .

# .env + credentials.json (copiar)
scp .env credentials.json verona@SEU_IP:/opt/verona-auto/

# Subir
docker compose up -d --build
docker compose logs -f

# Caddy precisa apontar pra container:porta (5050 no host com `ports:` do compose)
```

---

## Atualizar webhook do Evolution

Depois que o serviço estiver no ar em `https://oabprev.goaether.xyz`:

```bash
# Substitua os placeholders entre <...>
curl -X POST "<EVOLUTION_API_URL>/webhook/set/<INSTANCE_URL_ENCODED>" \
  -H "apikey: <EVOLUTION_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "webhook": {
      "url": "https://<SEU_DOMINIO>/webhook/evolution?token=<EVOLUTION_WEBHOOK_TOKEN>",
      "enabled": true,
      "events": ["MESSAGES_UPSERT"],
      "webhookByEvents": false,
      "webhookBase64": false
    }
  }'
```

## Smoke test

```bash
# Healthcheck
curl https://<SEU_DOMINIO>/health

# Status do disparo
curl https://<SEU_DOMINIO>/outreach/status

# Forçar 1 envio agora
curl -X POST https://<SEU_DOMINIO>/outreach/dispatch-now
```
