# SETUP — OABPrev Scraper Web

## 1. Criar e ativar `venv`

```bash
cd ~/Desktop/Custom/OabPrev/oab_scraper
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

Se a pasta `.venv` já existir, basta ativá-la:

```bash
source .venv/bin/activate
```

Se você estiver em macOS e o Playwright falhar com `SIGSEGV` usando Python `3.14`, recrie o ambiente com Python `3.12` ou `3.13`.

## 2. Instalar dependências

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## 3. Criar a Service Account no Google Cloud

1. Acesse o Google Cloud Console.
2. Crie um projeto ou use um existente.
3. Ative as APIs **Google Sheets API** e **Google Drive API**.
4. Vá em **Credenciais** → **Criar credenciais** → **Conta de serviço**.
5. Gere uma chave JSON.
6. Salve o arquivo como `credentials.json` na pasta do projeto ou envie pelo frontend.

## 4. Compartilhar a planilha

1. Abra o `credentials.json`.
2. Copie o valor de `client_email`.
3. Compartilhe a planilha `AGENDA OABPREV` com esse e-mail.
4. Dê permissão de **Editor**.

## 5. Subir a interface web local

```bash
source .venv/bin/activate
python app.py
```

Abra no navegador:

```text
http://127.0.0.1:5000
```

## 6. Fluxo esperado no frontend

1. Envie o `credentials.json`.
2. Valide o acesso à planilha.
3. Confira a contagem de linhas brancas detectadas.
4. Clique em **Iniciar processamento**.
5. Acompanhe os logs e o status em tempo real.
6. Confira os resultados na aba `Leads_OAB_Scraper`.

## 7. Execução opcional via CLI

Também é possível rodar sem frontend:

```bash
source .venv/bin/activate
python scraper.py --limit 10
```

Opções úteis:

```bash
python scraper.py --spreadsheet-id 1QRnMXp8lTmIefhHnP5LdSoin1QMj_imewcf3RAh_KzI --seccional "Santa Catarina" --limit 20
```

## Observações

- O frontend salva o arquivo recebido como `credentials.json` na pasta do projeto.
- O scraper usa Playwright em modo headless para consultar o CNA OAB.
- Os leads são filtrados pelo fundo branco da coluna de nome.
- Um backup local é salvo em `resultados_oab.json` após cada execução.
- Se o layout do CNA OAB mudar, os seletores centralizados em `scraper.py` podem precisar de ajuste.
