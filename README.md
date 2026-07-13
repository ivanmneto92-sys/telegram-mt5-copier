# Telegram MT5 Copier

Base portavel em Python 3.12 para desenvolvimento no macOS e execucao continua em uma VPS com Windows Server.

O projeto nao contem credenciais reais. Configure dados sensiveis somente em `.env`, nunca no codigo e nunca no GitHub.

## Configuracao

Copie `.env.example` para `.env` no ambiente onde a aplicacao sera executada:

```env
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
SOURCE_CHAT_ID=
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

O primeiro login deve ser feito manualmente no ambiente onde a sessao sera usada. Preencha `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `SOURCE_CHAT_ID` e `DESTINATION_CHAT_ID` no `.env` local desse ambiente.

Depois do setup, execute uma unica vez:

```bash
python -m telegram_mt5_copier --telegram-login
```

No Windows, use:

```powershell
.\.venv\Scripts\python.exe -m telegram_mt5_copier --telegram-login
```

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
