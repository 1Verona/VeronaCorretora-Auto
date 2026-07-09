# Verona Corretora Auto — Painel de Controle

Sistema inteligente de captação de leads e automação de disparos/atendimento para corretores de seguros OABPrev.

---

## 🔒 Credenciais de Acesso ao Painel

Por motivos de segurança, o painel agora possui uma tela de autenticação. Use as seguintes credenciais padrão (configuradas no seu arquivo `.env`):

*   **Usuário:** `Admin`
*   **Senha:** `123123@oabPrev`

---

## 🚀 Como Iniciar o Sistema (Local)

### 1. Iniciar o Servidor Backend (Flask + Bot)
Certifique-se de que seu ambiente virtual esteja ativo e com as dependências instaladas, depois execute:
```bash
python app.py
```
*O backend rodará automaticamente na porta `5050` ou na próxima disponível.*

### 2. Iniciar o Frontend (Vite + React)
Acesse a pasta do frontend, instale as dependências se necessário, e inicie o servidor de desenvolvimento:
```bash
cd frontend
npm run dev
```
*O frontend abrirá automaticamente em `http://localhost:3000`.*

---

## 📱 Configuração do WhatsApp / Evolution API
Agora você não precisa configurar o arquivo `.env` manualmente para trocar de celular ou instância. Tudo é feito direto no painel:
1. Abra o painel no navegador (`http://localhost:3000`).
2. Faça login com as credenciais acima.
3. Role até o card **WhatsApp / Evolution** e clique para expandir.
4. Preencha a URL da API, API Key e o Nome da Instância.
5. Clique em **Salvar**.
6. Clique em **Gerar QR Code** para escanear com o seu WhatsApp e conectar o aparelho.

---

## 📦 Hospedagem e Deploy em Produção
Para hospedar o sistema de forma persistente 24/7 (para que o bot e os disparos rodem mesmo com o seu computador desligado), consulte as instruções detalhadas em [Hosting.md](Hosting.md).
