# Hosting Guide — Verona Corretora Auto

> Documento de referência para deploy completo do sistema (backend Flask + Bot Telegram + frontend estático) em uma VM.

---

## 1. Visão Geral

Atualmente o bot do Telegram roda junto com o backend Flask no mesmo processo Python local (`app.py`). Isso significa que **se o backend cair, o bot cai junto**, e o bot ainda depende de endpoints internos do Flask para comandos como `/run`, `/stop` e `/job`.

A solução é hospedar **toda a stack na VM**, garantindo que o bot fique online 24/7 independente do app Electron do usuário estar aberto ou não.

**Arquitetura alvo:**

```
┌─────────────────────────────────────────────────────────────┐
│                         VM (Ubuntu/Debian)                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  Nginx       │  │  Flask API   │  │  Bot Telegram    │  │
│  │  (reverse    │◄─┤  (app.py)    │◄─┤  (telegram_bot)  │  │
│  │   proxy +    │  │  Porta 5050  │  │  (mesmo processo)│  │
│  │   frontend)  │  │              │  │                  │  │
│  │  Porta 80/443│  └──────────────┘  └──────────────────┘  │
│  └──────────────┘                                           │
└─────────────────────────────────────────────────────────────┘
         ▲
         │
    Usuário (navegador ou app Electron)
```

---

## 2. O que será hospedado

| Componente | Descrição | Porta Interna | Porta Pública |
|------------|-----------|---------------|---------------|
| Flask API | Backend com endpoints REST + gerenciador de jobs | 5050 | Apenas local (127.0.0.1) |
| Bot Telegram | Instanciado dentro do `app.py`, polling contínuo | — | Conecta via HTTPS nas APIs do Telegram |
| Frontend Build | Arquivos estáticos do Vite (`dist/`) | — | 80/443 via Nginx |

> **Nota:** O bot não será desacoplado do Flask nesta fase. Ele continuará no mesmo processo, mas rodando na VM em vez da máquina local.

---

## 3. Requisitos da VM

**Mínimo recomendado:**
- **SO:** Ubuntu 22.04 LTS ou Debian 12
- **CPU:** 1 vCPU
- **RAM:** 1 GB (2 GB se houver muitos jobs simultâneos)
- **Disco:** 10 GB SSD
- **Python:** 3.11+ (evitar warnings do Google API)
- **Node.js:** 18+ (apenas para build do frontend)

**Dependências do sistema:**
```bash
sudo apt update
sudo apt install -y python3-pip python3-venv nginx git nodejs npm
```

---

## 4. Estrutura de arquivos no servidor

```
/opt/verona-auto/
├── .env                        # Variáveis de ambiente (protegido)
├── .venv/                      # Ambiente virtual Python
├── app.py                      # Entrypoint principal
├── requirements.txt            # Dependências Python
├── credentials.json            # Service Account do Google (upload manual)
├── bot_contexts.json           # Persistência de contextos do bot
├── telegram_bot.py
├── scraper.py
├── sheet_manager.py
├── llm_router.py
├── nlp_engine.py
├── frontend/
│   ├── dist/                   # Build de produção (npm run build)
│   └── ... (source opcional)
├── cache/
└── logs/
    ├── backend.log             # stdout do Flask + bot
    └── nginx/
```

---

## 5. Passo a passo do deploy

### 5.1. Clonar o repositório

```bash
cd /opt
sudo git clone <repo-url> verona-auto
sudo chown -R $USER:$USER verona-auto
cd verona-auto
```

### 5.2. Configurar variáveis de ambiente

Criar o arquivo `.env` com os valores reais:

```bash
cp .env.example .env
nano .env
```

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
TELEGRAM_BOT_TOKEN=8457276789:...
```

> ⚠️ **Nunca commitar este arquivo.** Adicionar `.env` e `credentials.json` ao `.gitignore`.

### 5.3. Ambiente virtual e dependências Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

### 5.4. Build do frontend

```bash
cd frontend
npm install
npm run build
cd ..
```

O diretório `frontend/dist/` conterá os arquivos estáticos de produção.

### 5.5. Upload do credentials.json

```bash
scp credentials.json usuario@vm:/opt/verona-auto/
```

Validar permissões:
```bash
chmod 600 /opt/verona-auto/credentials.json
chmod 600 /opt/verona-auto/.env
```

---

## 6. Execução do backend (systemd)

Criar um serviço systemd para manter o processo vivo e iniciar automaticamente:

```bash
sudo nano /etc/systemd/system/verona-backend.service
```

```ini
[Unit]
Description=Verona Corretora Auto — Backend + Bot Telegram
After=network.target

[Service]
Type=simple
User=verona
Group=verona
WorkingDirectory=/opt/verona-auto
Environment="PATH=/opt/verona-auto/.venv/bin"
EnvironmentFile=/opt/verona-auto/.env
ExecStart=/opt/verona-auto/.venv/bin/python /opt/verona-auto/app.py
Restart=always
RestartSec=5
StandardOutput=append:/opt/verona-auto/logs/backend.log
StandardError=append:/opt/verona-auto/logs/backend.log

[Install]
WantedBy=multi-user.target
```

Ativar:
```bash
sudo mkdir -p /opt/verona-auto/logs
sudo systemctl daemon-reload
sudo systemctl enable verona-backend
sudo systemctl start verona-backend
sudo systemctl status verona-backend
```

Verificar logs:
```bash
sudo tail -f /opt/verona-auto/logs/backend.log
```

---

## 7. Nginx (reverse proxy + frontend estático)

### 7.1. Configuração do site

```bash
sudo nano /etc/nginx/sites-available/verona
```

```nginx
server {
    listen 80;
    server_name vm-ip-ou-dominio;

    # Frontend estático
    location / {
        root /opt/verona-auto/frontend/dist;
        index index.html;
        try_files $uri $uri/ /index.html;
    }

    # Proxy para API Flask
    location /api/ {
        proxy_pass http://127.0.0.1:5050/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
```

> **Nota:** O prefixo `/api/` na URL externa mapeia para `/` no Flask. Isso evita conflito com o frontend que serve na raiz (`/`).

### 7.2. Ativar site

```bash
sudo ln -s /etc/nginx/sites-available/verona /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

---

## 8. Ajustes necessários no código

### 8.1. `app.py` — Bind público

O `app.py` atual usa `host="127.0.0.1"`. Na VM isso pode continuar assim (já que o Nginx acessa via localhost), mas o `telegram_bot.py` precisa de uma URL acessível caso algum comando precise ser chamado externamente.

**Nenhuma mudança obrigatória**, mas recomenda-se deixar o Flask escutando em `0.0.0.0` apenas se não usar Nginx como proxy. Com Nginx, `127.0.0.1:5050` é suficiente e mais seguro.

### 8.2. `frontend/vite.config.js` — Modo produção

No frontend em produção, as chamadas à API não passarão mais pelo proxy do Vite. O código do frontend deve usar caminhos relativos ou uma URL base configurada via variável de ambiente.

**Opção recomendada:** Criar um helper de API que detecta o ambiente:

```javascript
const API_BASE = import.meta.env.VITE_API_URL || '/api';
// Uso: fetch(`${API_BASE}/status`)
```

No build de produção, definir `VITE_API_URL=/api` no `.env` do frontend ou passar na hora do build:

```bash
VITE_API_URL=/api npm run build
```

### 8.3. CORS no Flask

O `app.py` já permite CORS com `*`. Se o frontend for servido do mesmo domínio via Nginx, o CORS não é necessário, mas não prejudica.

---

## 9. Acesso pós-deploy

| Recurso | URL |
|---------|-----|
| Frontend | `http://vm-ip-ou-dominio` |
| API direta | `http://vm-ip-ou-dominio/api/health` |
| Bot Telegram | Funcionando via polling (não expõe porta) |

---

## 10. Manutenção

### Reiniciar backend
```bash
sudo systemctl restart verona-backend
```

### Ver logs
```bash
sudo journalctl -u verona-backend -f
# ou
sudo tail -f /opt/verona-auto/logs/backend.log
```

### Atualizar código
```bash
cd /opt/verona-auto
git pull
source .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm run build && cd ..
sudo systemctl restart verona-backend
sudo systemctl restart nginx
```

---

## 11. Próximos passos / melhorias futuras

- **HTTPS:** Configurar certificado com Let's Encrypt (`certbot`).
- **Docker:** Containerizar tudo (Flask + Nginx) para deploy ainda mais simples.
- **Desacoplar bot:** Extrair o bot para um serviço separado que consome a API Flask, permitindo restart independente.
- **Banco de dados:** Migrar de JSON local (`bot_contexts.json`) para PostgreSQL/SQLite se o volume de dados crescer.
- **CI/CD:** Automatizar o deploy com GitHub Actions ao dar push na branch `main`.

---

## 12. Checklist pré-deploy

- [ ] VM criada com acesso SSH
- [ ] `.env` configurado com tokens reais
- [ ] `credentials.json` da Google Service Account enviado para a VM
- [ ] `.env` e `credentials.json` no `.gitignore`
- [ ] Dependências Python instaladas (`requirements.txt`)
- [ ] Playwright Chromium instalado
- [ ] Frontend buildado (`npm run build`)
- [ ] Serviço systemd criado e ativado
- [ ] Nginx configurado e testado (`nginx -t`)
- [ ] Portas 80 (e 443 futuramente) liberadas no firewall da VM
- [ ] Teste de conexão: `curl http://vm-ip/api/health`
- [ ] Teste do bot: enviar `/start` no Telegram
