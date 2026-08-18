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
- desativar ou reativar canais de sinais sem apagar o histórico operacional;
- registrar cada alteração em `admin_actions`.

A partir da versão `0.31.0`, o painel é organizado em quatro áreas responsivas:
`Visão geral`, com pendências e atalhos; `Clientes`, com acesso, MT5 e operação;
`Financeiro`, com busca e filtros próprios para pagamentos e vencimentos; e
`Canais`, com solicitações e controle das fontes de sinais. No computador a
navegação usa menu lateral e, no celular, uma barra compacta no topo.

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
`📻 Canais de sinais`, ele pode enviar `@username`, um link `t.me` público, um
link privado de convite ou um link do Telegram Web.

O fluxo de segurança é:

1. o cliente sugere o canal;
2. o admin entra manualmente no canal com a conta técnica principal;
3. o monitor confirma a participação e o acesso ao histórico;
4. o admin analisa se o formato dos sinais é compatível;
5. o admin aprova o canal no painel;
6. o cliente escolhe seguir todos os canais aprovados ou apenas canais
   específicos.

Uma sugestão nunca faz a conta técnica entrar automaticamente em um canal e
nunca começa a executar ordens antes da aprovação. Em canais privados, o link
fica disponível somente na área administrativa e a conta técnica deve entrar
manualmente antes da verificação. O admin pode desativar um canal pelo painel:
ele deixa imediatamente de gerar novas ordens e desaparece da seleção dos
clientes, enquanto sinais e operações anteriores continuam preservados para
auditoria. Os canais já informados em `SOURCE_CHAT_IDS` são cadastrados como
fontes confiáveis ao iniciar o monitor, preservando a configuração existente;
um canal desativado no painel não é reativado automaticamente após reinícios.

Quando um sinal é aceito, a seleção do canal é aplicada individualmente antes
da escolha das contas MT5. Clientes no modo personalizado não recebem execução
de canais desmarcados. O painel administrativo mostra solicitações aguardando a
entrada da conta principal, canais prontos para análise e canais ativos.

O analisador também reconhece mensagens em português como `Moeda: XAU-USD`,
`Análise: Venda (Sell)`, `Entrada`, `Stop Loss (SL)` e listas com vários
`Take Profit (TP)`. `Compra`/`Venda` são normalizados para `BUY`/`SELL`, e
`XAU-USD` continua sendo resolvido para o símbolo específico da corretora, como
`XAUUSDb` na HFM.

Além do ouro, o analisador aceita pares Forex formados por AUD, CAD, CHF, EUR,
GBP, JPY, NZD e USD, como `CADCHF`, `EURUSD`, `GBPJPY` e `AUDJPY`. Separadores
como `CAD/CHF` e `CAD-CHF` são normalizados. O resolvedor consulta cada terminal
MT5 e preserva automaticamente sufixos da corretora, por exemplo `CADCHFb`,
`EURUSD.r` ou `GBPJPY_i`. Índices e criptomoedas permanecem bloqueados.
Relatórios com expressões como `closed trades` e listas de resultados em pips
são ignorados, mesmo quando contêm símbolos, BUY/SELL, TP e SL.

Os nomes técnicos dos fornecedores não são exibidos aos clientes. O menu do bot
usa aliases estáveis como `Sala de Sinais 01`, mantendo seleção e execução
vinculadas ao ID numérico real do Telegram. No painel administrativo, o admin
continua vendo título, username e chat ID verdadeiros e pode definir um nome
público personalizado para cada sala. Alterar ou remover o alias não modifica o
monitoramento, as assinaturas dos clientes nem o histórico de sinais.

Uma conta MT5 recém-conectada permanece aguardando aprovação. O cliente não
consegue se autoativar pelo bot. Somente o admin pode liberar sinais, registrando
o pagamento e definindo a data final do acesso. Depois do vencimento, a conta é
retirada da seleção de novos sinais; o Worker MT5 continua disponível para
acompanhar operações que já existiam.

## Alertas e resultados em tempo real

O Worker reconcilia os deals encerrados pelo MT5 a cada poucos segundos. Para
cada ordem do copiador, ele identifica TP, Stop Loss, breakeven, stop out ou
fechamento manual, calcula o resultado líquido incluindo comissão, swap e taxas
e envia uma mensagem privada ao dono da conta. A mensagem também informa o
resultado diário do robô e o resultado total da conta. O ticket do deal é salvo
com chave única no banco para impedir que a mesma leitura do histórico gere
avisos repetidos.

O cliente pode ativar ou desativar esses avisos em
`Configurações > Alertas de resultados`. Os últimos fechamentos ficam em
`Resultados`. O menu principal foi mantido curto; risco, execução, proteções,
alertas e contas MT5 estão reunidos em `Configurações`.

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

A Mini App de conexão deve ficar publicada atrás de HTTPS. `ONBOARDING_HOST` e
`ONBOARDING_PORT` definem o endereço local (por padrão `127.0.0.1:8080`).
Publique essa porta com Caddy, IIS, Nginx ou outro proxy HTTPS. Configure a URL
HTTPS final em `MT5_ONBOARDING_URL` e também no BotFather como Web App do botão
de conexão.

Gere a chave de criptografia diretamente na VPS:

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Salve o valor em `MT5_CREDENTIAL_KEY` no `.env` da VPS. Nunca envie essa chave ao GitHub.

Configure no `.env` da VPS:

```env
MT5_TEMPLATE_PATH=C:\Caminho\Para\MT5Modelo
MT5_BROKER_TEMPLATES=HFM=C:\MT5TemplateHFM;FTMO=C:\MT5TemplateFTMO;FXGLOBE=C:\MT5TemplateFXGlobe;EXNESS=C:\MT5TemplateExness;INFINOX=C:\MT5TemplateINFINOX
MT5_BROKER_SERVERS=HFM=HFMarketsGlobal-Live1|HFMarketsGlobal-Live2|HFMarketsGlobal-Live3;FTMO=FTMO-Demo
MT5_BASE_DIR=C:\MT5Accounts
MT5_EXECUTION_MODE=simulation
ALLOW_LIVE_ACCOUNTS=false
GLOBAL_EXECUTION_KILL_SWITCH=true
MT5_ONBOARDING_URL=https://seu-dominio/mt5
```

O script `scripts/setup_windows.ps1` instala o pacote `MetaTrader5` somente no Windows. No macOS, o projeto continua sem essa dependência.

Cada conta cadastrada recebe uma pasta isolada em `MT5_BASE_DIR\<mt5_account_id>\`, com `terminal64.exe`, `data\`, `logs\`, `worker.lock` e `heartbeat.txt`. A chamada ao MetaTrader usa modo portable, mantendo os dados junto da cópia isolada do terminal e evitando alternar contas dentro de um mesmo terminal.

Para operar com mais de uma corretora, mantenha uma instalação-modelo oficial e separada para cada uma e configure `MT5_BROKER_TEMPLATES`. A Mini App passa a exibir somente as corretoras configuradas e o provisionamento copia o modelo correspondente. `MT5_TEMPLATE_PATH` continua aceito como modelo HFM por compatibilidade com instalações anteriores. FTMO, FXGlobe, Exness e INFINOX podem usar servidores e símbolos diferentes; o resolvedor de símbolos detecta automaticamente sufixos disponíveis no terminal.

A partir da versão `0.29.0`, depois de escolher a corretora o cliente seleciona
um servidor da lista ou usa `Meu servidor não está na lista — digitar`. O
catálogo combina servidores oficiais conhecidos, todos os arquivos `*.srv`
encontrados na instalação-modelo, servidores que já tiveram contas cadastradas
e os nomes opcionais de `MT5_BROKER_SERVERS`. A lista padrão da HFM inclui os
servidores Live e Demo publicados pela corretora. Para FTMO e outras corretoras,
cujo servidor pode variar conforme a credencial, o cliente deve informar
exatamente o nome exibido no e-mail ou painel da conta. O backend aceita a opção
manual de forma controlada e continua validando as opções do catálogo.

Ao criar uma pasta a partir do template selecionado, o provisionamento remove `config\accounts.dat`, credenciais, bases e logs herdados antes da primeira inicialização. A configuração higienizada usa `KeepPrivate=0`, habilita negociação algorítmica, preserva as opções técnicas da API Python já validadas no template e não contém login ou senha. A conexão Python inicia o terminal diretamente com a conta do cliente. Falhas transitórias `IPC timeout`/`IPC send failed`, comuns durante o primeiro LiveUpdate, recebem apenas uma nova tentativa controlada. Se o cliente repetir um cadastro que falhou, o registro e a pasta existentes são reutilizados em vez de consumir outro ID da VPS.

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

A partir da versão `0.37.1`, atingir a meta diária ou o limite diário pausa
novos sinais imediatamente, mesmo quando o negócio que cruzou o limiar
encerrou a última posição aberta da conta. Antes, a trava financeira só
verificava o resultado do dia quando havia posição ou ordem pendente aberta
na varredura do Worker; se o Take Profit fechasse a única operação da conta
naquele instante, a meta diária ficava sem detectar o próprio acionamento até
o próximo sinal tentar entrar — sem aviso no Telegram e sem `daily_signal_pause_until`
registrado nesse intervalo.

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
confirmação, a retomada ocorre automaticamente na próxima abertura das 23:00 no
horário de Brasília. Acionamentos a partir das 23:00 aguardam a abertura do dia
seguinte; sexta-feira e sábado aguardam domingo às 23:00. O cliente também pode
usar `▶️ Retomar sinais agora` para remover a parada antes desse horário.

O parser também reconhece entradas escritas no cabeçalho como
`GOLD BUY NOW IN ZONE 4027-4020`. Alvos sem preço, como `TP 3: OPEN`, são
ignorados; os demais TPs numéricos continuam sendo executados normalmente.

## Proteção contra sinais duplicados no canal limpo

A partir da versão `0.28.2`, a deduplicação da publicação considera todas as
salas de origem. Se duas salas republicarem exatamente a mesma estrutura de
ativo, direção, entrada, SL e TPs dentro de quatro horas, o canal limpo recebe
somente uma mensagem. O segundo evento continua sendo registrado por origem
para respeitar a escolha de salas. Antes de executar, há uma segunda reserva por
conta: clientes que seguem as duas salas recebem uma única operação, enquanto
quem segue apenas a segunda sala continua recebendo o sinal normalmente.

O Monitor de Sinais também usa um lock exclusivo por instância. Uma segunda
cópia manual ou órfã é bloqueada antes de se conectar ao Telegram.

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

## Proteção confirmada após o TP1

A partir da versão `0.25.0`, o breakeven após o TP1 é acompanhado por posição.
O Worker identifica a operação primeiro pelo ticket salvo no banco e usa o
comentário somente como alternativa, protegendo também operações cujo comentário
foi abreviado ou alterado pela corretora. Depois de enviar o novo Stop, o sistema
relê a posição no MT5 e só registra o BE como concluído quando todas as posições
restantes estiverem confirmadas.

Falhas ficam registradas individualmente e geram um aviso no Telegram com a
recomendação de proteção manual. Se o TP1 já tiver sido atingido, mas o preço
voltar à entrada antes de o MT5 confirmar o novo Stop, a posição restante é
encerrada a mercado para limitar a exposição. Spread, comissão e slippage podem
fazer esse encerramento ficar ligeiramente diferente de zero. O gerenciamento
de posições existentes permanece ativo mesmo quando o usuário ou o perfil está
pausado para novas entradas.

## Identificação da sala no MetaTrader 5

A versão `0.26.0` substitui o comentário técnico isolado pelo nome público da
sala configurado no catálogo. Uma operação pode aparecer como
`Gold Alpha 6e87346a T1`, em que `Gold Alpha` é o nome mascarado mostrado ao
cliente, `6e87346a` é o identificador curto do sinal e `T1` indica o alvo.

O comentário é limitado automaticamente a 31 caracteres para compatibilidade
com o MT5. O nome original do canal não é exposto. A identificação operacional
continua usando prioritariamente os tickets gravados no banco, e o novo e o
antigo formato de comentário permanecem reconhecidos para não afetar posições
criadas por versões anteriores.

## Instâncias white-label na mesma VPS

A versão `0.18.0` permite executar cópias independentes para marcas diferentes.
Cada cópia usa o mesmo código, os mesmos IDs de canais de origem e os mesmos
templates MT5, mas deve possuir bot, banco, sessão Telegram, porta HTTP, domínio,
diretório de contas e tarefa agendada próprios.

Exemplo da instância principal:

```env
INSTANCE_ID=main
BRAND_NAME=Instituto Trader
DATA_DIR=./data
SESSION_DIR=./sessions
LOG_DIR=./logs
ONBOARDING_HOST=127.0.0.1
ONBOARDING_PORT=8080
MT5_BASE_DIR=C:\MT5Accounts\principal
MT5_ONBOARDING_URL=https://institutotrader.online
```

Exemplo de uma segunda marca, em outra cópia do projeto:

```env
INSTANCE_ID=mesa_alpha
BRAND_NAME=Mesa Alpha
DATA_DIR=./data
SESSION_DIR=./sessions
LOG_DIR=./logs
ONBOARDING_HOST=127.0.0.1
ONBOARDING_PORT=8081
MT5_BASE_DIR=C:\MT5Accounts\mesa_alpha
MT5_ONBOARDING_URL=https://app.mesaalpha.com
```

`SOURCE_CHAT_IDS` e `MT5_BROKER_TEMPLATES` podem ter os mesmos valores nas duas
instâncias. `TELEGRAM_BOT_TOKEN` deve ser de outro bot e a segunda instância deve
executar `telegram-login`, criando sua própria sessão de monitoramento. Nunca
aponte duas instâncias para o mesmo `MT5_BASE_DIR`, banco ou `SESSION_DIR`.

A partir da versão `0.37.0`, cada instância pode sincronizar aprovações de
canal com uma instância irmã. Por padrão, cada instância mantém seu próprio
catálogo de canais: aprovar ou
renomear uma sala no painel de uma marca não altera a outra, mesmo quando as
duas monitoram o mesmo canal real do Telegram. Para sincronizar isso
automaticamente, defina `PEER_CHANNEL_SYNC_DATABASES` no `.env` de cada
instância com o caminho do arquivo `.sqlite3` da(s) instância(s) irmã(s):

```env
# Na instância main
PEER_CHANNEL_SYNC_DATABASES=C:\Apps\telegram-mt5-copier-robo_braba\telegram-mt5-copier\data\telegram_mt5_copier_robo_braba.sqlite3

# Na instância robo_braba
PEER_CHANNEL_SYNC_DATABASES=C:\Apps\telegram-mt5-copier-main\telegram-mt5-copier\data\telegram_mt5_copier.sqlite3
```

Com isso, aprovar, suspender ou renomear um canal em uma instância replica a
mudança na instância peer, casando pelo `telegram_chat_id` real do canal. A
sincronização nunca cria um canal do zero na outra instância nem o ativa antes
da hora: se a conta técnica da instância peer ainda não confirmou acesso
àquele canal (o "admin entra manualmente no canal" continua sendo por
instância), só o apelido é sincronizado e a aprovação fica pendente até essa
confirmação acontecer também lá. Uma instância peer temporariamente
indisponível nunca bloqueia a aprovação na instância de origem.

Isso cobre só as próximas aprovações. Canais que já haviam divergido antes de
`PEER_CHANNEL_SYNC_DATABASES` existir continuam divergentes até uma
reconciliação manual. Depois de configurar a variável nas duas instâncias,
rode uma vez em cada uma delas:

```powershell
.\.venv\Scripts\python.exe -m telegram_mt5_copier --sync-channels
```

O comando lista os canais ativos localmente e, para cada um, o resultado em
cada instância peer: sincronizado e ativado, apelido sincronizado mas
aguardando a conta técnica do peer confirmar acesso, ou o peer ainda nem
conhece aquele canal (nesse caso, o admin entra manualmente com a conta
técnica daquela instância no canal antes de repetir o comando). Rode o
`--sync-channels` nas duas direções — o que existe só no Instituto Trader
precisa ser rodado a partir dele, e vice-versa.

O script `install_windows_startup.ps1` lê `INSTANCE_ID`: a instância `main`
mantém a tarefa `Telegram MT5 Copier`; as demais recebem automaticamente nomes
como `Telegram MT5 Copier - mesa_alpha`. Cada porta local precisa de uma rota
HTTPS própria no proxy reverso.

Para atualizações futuras no Windows, use `scripts\update_windows_instance.ps1`
dentro de cada cópia. O script encerra a tarefa e toda a árvore de processos
daquela instância antes do `git pull`, evitando Workers órfãos e disputas pelo
arquivo `worker.lock`. Os processos de outra marca e os terminais MT5 não são
encerrados.

## Portal do cliente

Configure `CLIENT_APP_URL` com o endereço HTTPS do frontend. O cliente pode
entrar por um link descartável emitido pelo bot ou por e-mail e senha. Senhas
são armazenadas somente como hash `scrypt`; após cinco tentativas inválidas, o
acesso fica bloqueado por 15 minutos.

Cadastros feitos diretamente no portal são criados com usuário pausado e
financeiro pendente. Eles aparecem no painel administrativo e somente passam a
receber sinais depois da aprovação e da conexão de uma conta MT5. Se o e-mail
já pertence a um cliente cadastrado pelo Telegram, o portal não cria uma conta
duplicada: esse cliente deve entrar pelo bot e configurar o acesso web na sua
sessão autenticada.

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

## Proteção contra notícias de alto impacto

A partir da versão `0.27.2`, o menu **Configurações > Notícias do mercado**
permite que cada cliente escolha entre operar normalmente ou bloquear novas
entradas durante notícias de alto impacto. A opção padrão é operar normalmente;
portanto, nenhum cliente é bloqueado sem escolher a proteção.

Quando ativada, a proteção considera apenas a moeda relacionada ao ativo. Por
exemplo, uma notícia forte dos Estados Unidos afeta `XAUUSD` e `EURUSD`, mas não
bloqueia `EURGBP`. A janela padrão começa 10 minutos antes e termina 10 minutos
depois do evento. Posições e ordens já existentes não são alteradas.

Clientes ativos e com MT5 conectado recebem um aviso 10 minutos antes e outro
no momento do evento. Por padrão, o calendário usa gratuitamente a exportação
semanal oficial do Forex Factory, sem cadastro, cartão ou chave de API:

```dotenv
MARKET_NEWS_ENABLED=true
MARKET_NEWS_MINUTES_BEFORE=10
MARKET_NEWS_MINUTES_AFTER=10
MARKET_NEWS_POLL_SECONDS=30
```

`ECONOMIC_CALENDAR_API_KEY` é opcional. Se preenchida, troca o provedor gratuito
pela Trading Economics; se ficar vazia, o Forex Factory é usado normalmente.

Se o provedor estiver indisponível, o sistema registra o erro e opera em modo
aberto: ele não inventa eventos nem bloqueia entradas sem informação válida.
O supervisor inicia o componente `market-news` somente quando
`MARKET_NEWS_ENABLED=true`.

## Leitura segura de sinais desenhados em imagens

A partir da versão `0.34.0`, canais previamente analisados podem usar OCR local
quando o Telegram entrega uma legenda corrompida por custom emojis. A leitura é
opt-in por ID de canal e utiliza o Tesseract instalado na própria VPS, sem enviar
a imagem ou os dados da operação para uma API externa.

O OCR não é aceito sozinho: entrada, Stop Loss e pelo menos dois Take Profits
extraídos da imagem também precisam aparecer numericamente na legenda original.
Se a validação cruzada falhar, o sinal é rejeitado e nenhuma ordem é enviada.

```dotenv
TELEGRAM_IMAGE_OCR_ENABLED=true
TELEGRAM_IMAGE_OCR_CHAT_IDS=-1002849262979
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
```

Não habilite OCR globalmente para canais ainda não analisados. Cada novo formato
de imagem deve ser testado antes de seu ID ser incluído na configuração.

A partir da versão `0.38.0`, existe um segundo modo de recuperação por OCR
para salas cujo emoji customizado apaga só a palavra BUY/SELL/COMPRA/VENDA da
legenda — por exemplo, quando a sala usa um selo colorido para "SELL" em vez
de escrever a palavra, e a imagem em si não traz entrada, SL nem TPs (só um
banner como "GOLD SELL"). Nesse caso o OCR da imagem inteira não basta para
formar um sinal completo, então o sistema lê da imagem somente a palavra de
direção e a reinsere no lugar exato onde o emoji estava, mantendo entrada,
Stop Loss e Take Profits vindos exclusivamente da legenda original, com as
mesmas validações de sempre. Se a legenda já tiver uma direção reconhecível,
nada é alterado; se o OCR não conseguir ler nenhuma palavra de direção na
imagem, o sinal continua sendo rejeitado. Esse modo usa a mesma configuração
de opt-in por canal acima — não é preciso nenhuma variável adicional.
