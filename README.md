# Telegram MT5 Copier

Base portavel em Python 3.12 para desenvolvimento no macOS e execucao continua em uma VPS com Windows Server.

O projeto nao contem credenciais reais. Configure dados sensiveis somente em `.env`, nunca no codigo e nunca no GitHub.

## Configuracao

Copie `.env.example` para `.env` no ambiente onde a aplicacao sera executada:

```env
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
SOURCE_CHAT_IDS=-1001111111111,-1002222222222
DESTINATION_CHAT_ID=
DRY_RUN=true
DATA_DIR=./data
SESSION_DIR=./sessions
LOG_DIR=./logs
```

As pastas `DATA_DIR`, `SESSION_DIR` e `LOG_DIR` podem ser relativas a raiz do projeto ou caminhos configurados por variavel de ambiente. A aplicacao cria essas pastas automaticamente.

Nao envie ao GitHub:

- API ID
- API Hash
- numero de telefone
- codigo recebido pelo Telegram
- senha de verificacao em duas etapas
- arquivo `.env`
- arquivos `.session`
- banco de dados
- logs

## 1. Desenvolvimento no macOS

Instale o Python 3.12 e execute:

```bash
cp .env.example .env
./scripts/setup_mac.sh
```

Mantenha `DRY_RUN=true` enquanto estiver preparando o ambiente sem credenciais reais.

## 2. Primeiro login no Telegram

O primeiro login deve ser feito manualmente no ambiente onde a sessao sera usada. Preencha `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `SOURCE_CHAT_IDS` e `DESTINATION_CHAT_ID` no `.env` local desse ambiente. Separe os IDs dos canais de origem por virgula; a conta da sessao deve participar de todos eles. A configuracao antiga `SOURCE_CHAT_ID` continua aceita quando `SOURCE_CHAT_IDS` nao estiver preenchido.

Depois do setup, execute uma unica vez:

```bash
python -m telegram_mt5_copier --telegram-login
```

No Windows, use:

```powershell
.\.venv\Scripts\python.exe -m telegram_mt5_copier --telegram-login
```

## Painel administrativo

Quando `BOT_ADMIN_IDS` e `MT5_ONBOARDING_URL` estão configurados, o menu do bot
exibe os botões administrativos exclusivamente para os IDs autorizados. O painel
abre em `/admin` tanto como Telegram Mini App quanto no navegador comum do PC.
No Telegram, ele valida o `initData` assinado. Para entrar pelo PC, o admin toca
em **Acessar pelo PC** e abre o link de uso único, válido por 5 minutos. O token
fica no fragmento da URL, não vai para os logs HTTP e é trocado por uma sessão
`HttpOnly`, `Secure` e `SameSite=Strict` de até 12 horas.

O admin master pode:

- consultar totais de clientes ativos, pausados e contas MT5 conectadas;
- buscar clientes por username, ID Telegram, conta, servidor, nome, e-mail,
  telefone ou plano;
- visualizar conta mascarada, saldo, equity, heartbeat e perfil operacional;
- identificar contas que precisam de atenção;
- ativar ou pausar clientes com confirmação;
- cadastrar nome, contato, plano, mensalidade e data de vencimento;
- identificar pagamentos em dia, vencimentos próximos e clientes em atraso;
- registrar pagamentos manualmente, avançar o próximo vencimento e consultar o
  histórico das últimas cobranças;
- aprovar o acesso somente ao registrar o valor pago e a data de validade;
- bloquear automaticamente novas entradas quando a validade terminar;
- visualizar a receita mensal cadastrada;
- registrar cada alteração em `admin_actions`.

Exemplo:

```env
BOT_ADMIN_IDS=8625829080
MT5_ONBOARDING_URL=https://institutotrader.online/
```

O painel não possui senha estática. O acesso pelo Telegram ou pelo link
temporário do bot é verificado novamente em cada chamada da API. Nesta versão,
os pagamentos são registrados manualmente pelo admin; integração automática
com PIX/cartão depende da escolha futura do provedor de pagamento.

Uma conta MT5 recém-conectada permanece aguardando aprovação. O cliente não
consegue se autoativar pelo bot. Somente o admin pode liberar sinais, registrando
o pagamento e definindo a data final do acesso. Depois do vencimento, a conta é
retirada da seleção de novos sinais; o Worker MT5 continua disponível para
acompanhar operações que já existiam.

Informe telefone, codigo recebido e senha de verificacao em duas etapas apenas no prompt interativo. A sessao autenticada fica em `SESSION_DIR` e os arquivos `*.session` ja estao bloqueados no `.gitignore`.

## 3. Execucao dos testes no Mac

Depois do setup:

```bash
source .venv/bin/activate
python -m unittest discover -s tests
```

## 4. Publicacao em um repositorio privado do GitHub

Crie um repositorio privado no GitHub. Antes do primeiro commit, confira se `.env`, sessoes, banco e logs nao aparecem no status:

```bash
git init
git status --short
git add .gitignore .env.example README.md pyproject.toml requirements.txt scripts src tests
git commit -m "Initial portable project setup"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/telegram-mt5-copier.git
git push -u origin main
```

Nunca execute `git add .env`, `git add data`, `git add logs` ou `git add sessions`.

## 5. Instalacao na VPS Windows

Instale o Python 3.12 no Windows Server. Durante a instalacao, habilite a opcao para adicionar Python ao `PATH`, ou use o launcher `py`.

Abra o PowerShell na pasta do projeto e, se necessario, permita a execucao do script somente para esta sessao:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## 6. Clonagem do repositorio na VPS

Clone o repositorio privado usando sua credencial do GitHub:

```powershell
git clone https://github.com/SEU_USUARIO/telegram-mt5-copier.git
cd telegram-mt5-copier
```

## 7. Criacao do ambiente virtual no Windows

Execute:

```powershell
.\scripts\setup_windows.ps1
```

O script verifica Python 3.12, cria `.venv`, instala o projeto e prepara as pastas configuradas.

## 8. Instalacao das dependencias

O setup do Windows ja instala as dependencias com:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

Repita esses comandos depois de alterar dependencias.

## 9. Configuracao do `.env` diretamente na VPS

Crie o `.env` na propria VPS:

```powershell
Copy-Item .env.example .env
notepad .env
```

Preencha os dados reais somente nesse arquivo. Para producao, ajuste `DRY_RUN=false` apenas depois de confirmar que as credenciais e chats estao corretos.

## 10. Inicializacao manual para teste

No Windows Server:

```powershell
.\scripts\start_windows.ps1
```

No macOS:

```bash
./scripts/start_mac.sh
```

O processo fica em execucao ate receber interrupcao. Use `Ctrl+C` no teste manual.

## 11. Inicio automatico apos reinicializacao da VPS

No Agendador de Tarefas do Windows:

1. Crie uma tarefa, nao uma tarefa basica.
2. Em Geral, marque para executar usando uma conta de servico ou usuario dedicado.
3. Marque "Executar estando o usuario conectado ou nao".
4. Marque "Executar com privilegios mais altos" se a instalacao do MetaTrader exigir.
5. Em Disparadores, adicione "Ao iniciar".
6. Em Acoes, use `powershell.exe` como programa.
7. Em argumentos, use `-NoProfile -ExecutionPolicy Bypass -File ".\scripts\start_windows.ps1"`.
8. Em "Iniciar em", informe a pasta raiz do projeto clonado na VPS.
9. Em Configuracoes, marque para reiniciar em caso de falha e escolha um intervalo, por exemplo 1 minuto.
10. Configure varias tentativas de reinicio para manter o servico ativo apos falhas temporarias.

Confirme que o diretorio de trabalho e a raiz do projeto, pois o script resolve `.env`, `.venv`, `DATA_DIR`, `SESSION_DIR` e `LOG_DIR` a partir dessa pasta.

## Fluxo MT5 completo na VPS Windows

A Mini App de conexão deve ficar publicada atrás de HTTPS. Na VPS, execute `telegram-mt5-onboarding` em `127.0.0.1:8080` e publique essa porta com IIS, Nginx ou outro proxy HTTPS. Configure a URL HTTPS final em `MT5_ONBOARDING_URL` e também no BotFather como Web App do botão de conexão.

Gere a chave de criptografia diretamente na VPS:

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Salve o valor em `MT5_CREDENTIAL_KEY` no `.env` da VPS. Nunca envie essa chave ao GitHub.

Configure no `.env` da VPS:

```env
MT5_TEMPLATE_PATH=C:\Caminho\Para\MT5Modelo
MT5_BASE_DIR=C:\MT5Accounts
MT5_EXECUTION_MODE=simulation
ALLOW_LIVE_ACCOUNTS=false
GLOBAL_EXECUTION_KILL_SWITCH=true
MT5_ONBOARDING_URL=https://seu-dominio/mt5
```

O script `scripts/setup_windows.ps1` instala o pacote `MetaTrader5` somente no Windows. No macOS, o projeto continua sem essa dependência.

Cada conta cadastrada recebe uma pasta isolada em `MT5_BASE_DIR\<mt5_account_id>\`, com `terminal64.exe`, `data\`, `logs\`, `worker.lock` e `heartbeat.txt`. A chamada ao MetaTrader usa modo portable, mantendo os dados junto da cópia isolada do terminal e evitando alternar contas dentro de um mesmo terminal.

Para validar uma conta na VPS:

1. Inicie `telegram-mt5-onboarding` atrás de HTTPS.
2. Inicie `telegram-management-bot`.
3. No Telegram, toque em `🔗 Conectar conta MT5`.
4. Cadastre corretora, servidor, login, senha e alias pela Mini App. A senha é enviada por POST, criptografada e nunca é exibida novamente.
5. Use `🖥️ Minhas contas` e `🔄 Testar conexão` para confirmar login, servidor, tipo Demo/Real, modo Hedging/Netting, saldo e equity.
6. Inicie `telegram-mt5-worker` para manter heartbeat e reconexão por conta.
7. Para envio em conta real, configure `MT5_EXECUTION_MODE=live_execution` e `ALLOW_LIVE_ACCOUNTS=true`.
8. O envio só é liberado quando `GLOBAL_EXECUTION_KILL_SWITCH=false`, a conta está conectada, o usuário está ativo e o perfil operacional está habilitado.

O modo real aplica antes do envio: lote fixo ou risco percentual, spread máximo, limite/meta diária, máximo de sinais, volume mínimo/step, distância mínima de stops e `order_check`. Ordens de múltiplos TPs são verificadas antes do primeiro envio; se uma submissão intermediária falhar, o serviço tenta remover as pendentes já criadas. Breakeven e trailing são administrados pelo `telegram-mt5-worker` após o preço avançar 1R.

No bot de gestão, cada campo de risco possui valores rápidos e a opção `✍️ Personalizado`. Após tocar nessa opção, envie o número como mensagem: lote (`0,07`), risco (`0,75%`), meta/limite (`$ 150`) ou valores inteiros para operações, spread e slippage. Saldo, equity, meta e limite são exibidos com `$`; entrada, SL e TP permanecem sem símbolo monetário porque representam cotações do ativo.

`DRY_RUN` controla a republicação no Telegram e não substitui as travas específicas do MT5.

Serviços sugeridos no Agendador de Tarefas ou serviço Windows dedicado:

- `telegram-management-bot`
- `telegram-mt5-onboarding`
- `telegram-copier`
- `telegram-mt5-worker`

Cada serviço deve usar a pasta raiz do projeto como diretorio de trabalho. Configure reinício automático em caso de falha.
