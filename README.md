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
- visualizar conta mascarada, saldo, equity, resultado realizado do dia em dólar
  e percentual sobre a banca inicial, heartbeat e perfil operacional;
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

## Catálogo de canais sugeridos pelos clientes

O monitoramento usa uma única conta técnica do Telegram. O cliente não conecta
a própria sessão e não adiciona canais diretamente à execução. Pelo submenu
`📻 Canais de sinais`, ele pode enviar `@username`, um link `t.me` público ou um
link do Telegram Web.

O fluxo de segurança é:

1. o cliente sugere o canal;
2. o admin entra manualmente no canal com a conta técnica principal;
3. o monitor confirma a participação e o acesso ao histórico;
4. o admin analisa se o formato dos sinais é compatível;
5. o admin aprova o canal no painel;
6. o cliente escolhe seguir todos os canais aprovados ou apenas canais
   específicos.

Uma sugestão nunca faz a conta técnica entrar automaticamente em um canal e
nunca começa a executar ordens antes da aprovação. Links privados de convite
não são armazenados; devem ser enviados diretamente ao administrador. Os canais
já informados em `SOURCE_CHAT_IDS` são cadastrados como fontes confiáveis ao
iniciar o monitor, preservando a configuração existente.

Quando um sinal é aceito, a seleção do canal é aplicada individualmente antes
da escolha das contas MT5. Clientes no modo personalizado não recebem execução
de canais desmarcados. O painel administrativo mostra solicitações aguardando a
entrada da conta principal, canais prontos para análise e canais ativos.

O analisador também reconhece mensagens em português como `Moeda: XAU-USD`,
`Análise: Venda (Sell)`, `Entrada`, `Stop Loss (SL)` e listas com vários
`Take Profit (TP)`. `Compra`/`Venda` são normalizados para `BUY`/`SELL`, e
`XAU-USD` continua sendo resolvido para o símbolo específico da corretora, como
`XAUUSDb` na HFM.

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

Ao criar uma pasta a partir de `MT5_TEMPLATE_PATH`, o provisionamento remove `config\accounts.dat`, credenciais, bases e logs herdados antes da primeira inicialização. A configuração higienizada usa `KeepPrivate=0`, habilita negociação algorítmica, preserva as opções técnicas da API Python já validadas no template e não contém login ou senha. A conexão Python inicia o terminal diretamente com a conta do cliente. Falhas transitórias `IPC timeout`/`IPC send failed`, comuns durante o primeiro LiveUpdate, recebem apenas uma nova tentativa controlada. Se o cliente repetir um cadastro que falhou, o registro e a pasta existentes são reutilizados em vez de consumir outro ID da VPS.

Para validar uma conta na VPS:

1. Inicie `telegram-mt5-onboarding` atrás de HTTPS.
2. Inicie `telegram-management-bot`.
3. No Telegram, toque em `🔗 Conectar conta MT5`.
4. Cadastre corretora, servidor, login, senha e alias pela Mini App. A senha é enviada por POST, criptografada e nunca é exibida novamente.
5. Use `🖥️ Minhas contas` e `🔄 Testar conexão` para confirmar login, servidor, tipo Demo/Real, modo Hedging/Netting, saldo e equity.
6. Inicie `telegram-mt5-worker` para manter heartbeat e reconexão por conta.
7. Para envio em conta real, configure `MT5_EXECUTION_MODE=live_execution` e `ALLOW_LIVE_ACCOUNTS=true`.
8. O envio só é liberado quando `GLOBAL_EXECUTION_KILL_SWITCH=false`, a conta está conectada, o usuário está ativo e o perfil operacional está habilitado.

O modo real aplica antes do envio: lote fixo ou risco percentual, spread máximo, limite/meta diária, máximo de sinais, volume mínimo/step, distância mínima de stops e `order_check`. Ordens de múltiplos TPs são verificadas antes do primeiro envio; se uma submissão intermediária falhar, o serviço tenta remover as pendentes já criadas.

Em `⚙️ Configurações > 🎯 Execução do sinal > Quantidade de TPs`, o usuário escolhe os primeiros 1, 2, 3 ou 4 alvos do sinal, ou todos os alvos disponíveis. O lote total configurado é dividido somente entre os TPs selecionados. A seleção não inventa alvos quando o sinal possui menos TPs.

O `telegram-mt5-worker` detecta quando o TP1 é atingido usando o preço atual e o histórico do MT5, inclusive após uma breve reconexão. Em `🛡️ Proteções > 🎯 BE após TP1`, cada cliente escolhe se o Stop Loss das posições restantes deve ser movido para o preço real de entrada. A preferência começa ativada para preservar o comportamento existente. Ordens daquele grupo que ainda não foram ativadas são canceladas após o TP1 para evitar entradas tardias em um sinal já desenvolvido. A proteção é aplicada apenas a posições do copiador, identificadas por `magic` e comentário; operações manuais não são alteradas. O BE antecipado em 1R e o trailing são preferências separadas e complementares.

Na tela `💼 Minha conta`, o resultado realizado do dia considera as operações
fechadas da conta (incluindo custos, swap e comissões), ignora depósitos e
saques e mostra `$ lucro/prejuízo (percentual)`. O percentual usa como base a
banca estimada no início do dia, calculada por `saldo atual - resultado
realizado`. O dia segue o horário de Brasília (UTC-3) e é atualizado pelo
`telegram-mt5-worker`.

O botão `🛑 Parar sinais hoje` bloqueia somente novas entradas do cliente e
mantém o Worker MT5 gerenciando ordens e posições existentes. Após uma
confirmação, a retomada ocorre automaticamente na próxima abertura das 20:00 no
horário de Brasília. Acionamentos depois das 20:00 aguardam a abertura do dia
seguinte; sexta-feira e sábado aguardam domingo às 20:00. O cliente também pode
usar `▶️ Retomar sinais agora` para remover a parada antes desse horário.

O parser também reconhece entradas escritas no cabeçalho como
`GOLD BUY NOW IN ZONE 4027-4020`. Alvos sem preço, como `TP 3: OPEN`, são
ignorados; os demais TPs numéricos continuam sendo executados normalmente.

## Modos de entrada por cliente

A partir da versão `0.13.0`, cada cliente escolhe no bot em
`Minhas contas > Configurar execução > Modo de entrada`:

- `🚀 Entrar imediatamente`: envia ordens a mercado usando o ask atual para
  BUY ou o bid atual para SELL, mesmo que a cotação esteja fora da zona;
- `📍 Posicionar na entrada`: cria ordens pendentes nos preços indicados pelo
  sinal;
- `⚖️ Mercado somente na zona`: executa a mercado se a cotação já estiver
  dentro da faixa e, caso contrário, posiciona as ordens.

A entrada imediata continua respeitando spread, slippage, margem, lote,
limites diários, SL e TPs. Se a cotação já tiver atingido o SL ou ultrapassado
o primeiro TP, o sinal é rejeitado em vez de abrir uma operação sem estrutura
válida. Vários TPs são enviados como posições separadas em contas hedging,
preservando a proteção de breakeven após o TP1. Se houver falha durante um
envio parcial a mercado, o executor tenta fechar imediatamente as posições
daquele sinal que já foram abertas.

## Supervisor e inicialização automática no Windows

A partir da versão `0.12.0`, o comando `telegram-mt5-supervisor` inicia e
acompanha estes cinco componentes:

- Mini App / cadastro MT5;
- Bot de gestão do Telegram;
- Monitor dos sinais;
- Worker MT5.
- Monitor operacional e alertas administrativos.

Se um processo encerrar, o supervisor reinicia com espera progressiva de
1, 2, 5, 10, 30 e no máximo 60 segundos. Cada componente escreve em um
arquivo separado dentro de `LOG_DIR`, com nomes
`supervisor-mini-app.log`, `supervisor-management-bot.log`,
`supervisor-signal-monitor.log`, `supervisor-mt5-worker.log` e
`supervisor-health-monitor.log`. O próprio supervisor usa
`telegram-mt5-supervisor.log`.

O monitor operacional verifica a cada 30 segundos o heartbeat do monitor de
sinais e da conta MT5 mais recente habilitada de cada cliente. Quando uma
conexão falha, o heartbeat fica atrasado ou um dos processos supervisionados
encerra, os IDs configurados em `BOT_ADMIN_IDS` recebem um alerta pelo bot.
O problema fica registrado no SQLite para não repetir mensagens; por padrão,
um problema contínuo só é lembrado novamente depois de 360 minutos. Quando o
estado normaliza, o administrador recebe uma confirmação de recuperação.

As opções podem ser ajustadas no `.env`:

```dotenv
OPERATIONAL_ALERTS_ENABLED=true
HEALTH_CHECK_INTERVAL_SECONDS=30
HEALTH_STALE_AFTER_SECONDS=90
OPERATIONAL_ALERT_REPEAT_MINUTES=360
```

Essas notificações usam o mesmo `TELEGRAM_BOT_TOKEN` do bot de gestão e são
enviadas somente para `BOT_ADMIN_IDS`. Não existe conflito de `getUpdates`,
pois o monitor apenas chama `sendMessage`.

O Caddy não é iniciado pelo supervisor: ele continua usando a configuração
HTTPS já existente.

Como o MetaTrader 5 precisa da sessão gráfica do Windows, a automação usa uma
tarefa no login do usuário da VPS, e não a conta `SYSTEM` da sessão 0. Depois
do login, a conexão RDP pode ser apenas desconectada; não use **Sair**, pois
isso encerra a sessão gráfica dos terminais MT5.

Antes da primeira instalação, encerre as instâncias iniciadas
manualmente. Em um PowerShell como Administrador, execute:

```powershell
cd C:\Apps\telegram-mt5-copier-main\telegram-mt5-copier
.\scripts\install_windows_startup.ps1
```

Depois disso, não abra os executáveis individualmente. Para consultar
a tarefa:

```powershell
Get-ScheduledTask -TaskName 'Telegram MT5 Copier' |
    Select-Object TaskName, State
```

Para remover somente a inicialização automática e parar o supervisor:

```powershell
.\scripts\uninstall_windows_startup.ps1
```

Quando o cliente ativa a proteção após TP1, o Worker MT5 precisa permanecer ligado. Como a alteração do stop é executada pela API na VPS, indisponibilidade prolongada da VPS, do terminal ou da corretora pode impedir a proteção no instante exato; para garantia totalmente independente da VPS seria necessário também um Expert Advisor executado dentro do terminal.

No bot de gestão, cada campo de risco possui valores rápidos e a opção `✍️ Personalizado`. Após tocar nessa opção, envie o número como mensagem: lote (`0,07`), risco (`0,75%`), meta/limite (`$ 150`) ou valores inteiros para operações, spread e slippage. Saldo, equity, meta e limite são exibidos com `$`; entrada, SL e TP permanecem sem símbolo monetário porque representam cotações do ativo.

`DRY_RUN` controla a republicação no Telegram e não substitui as travas específicas do MT5.

Com o supervisor instalado, não cadastre cada executável separadamente no
Agendador de Tarefas. A única tarefa automática deve iniciar
`telegram-mt5-supervisor`.
