# Guia dos menus do bot Telegram

Este documento descreve os menus disponíveis no bot privado de gestão do Instituto Trader e do Robô Braba. As duas marcas utilizam a mesma estrutura funcional; cada instância mantém seus próprios clientes, configurações, contas MT5 e banco de dados.

## Visão geral do sistema

O sistema recebe sinais de canais aprovados, interpreta ativo, direção, faixa de entrada, Stop Loss e Take Profits, e cria operações nas contas MT5 autorizadas. Antes de executar, ele verifica a situação do cliente, assinatura, conexão da conta, preferências de canais, modo de entrada e proteções de risco.

O bot Telegram é o painel particular do cliente. Por ele, o usuário consulta a conta, acompanha operações e resultados, escolhe canais e configura como os sinais serão executados. O bot não é o canal que fornece os sinais: ele é a interface de controle do copiador.

## Menu principal

### 💼 Minha conta

Mostra um resumo da conta principal do usuário:

- status do copiador;
- situação da conexão com o MetaTrader 5;
- liberação ou bloqueio de novas operações;
- saldo e equity;
- resultado financeiro do dia.

O resultado diário é líquido e considera os dados fornecidos pelo MT5, incluindo lucro ou prejuízo, comissão, swap e outras taxas. O botão **Atualizar** consulta novamente as informações registradas pelo Worker MT5.

### 📈 Operações

Exibe as operações ativas que foram criadas pelo copiador. Para cada grupo de sinal, apresenta informações como ativo, direção, lote total, entrada, Stop Loss, quantidade de TPs e estado da execução.

Essa tela acompanha somente as operações controladas pelo sistema. Ordens manuais do cliente não são apresentadas como operações do copiador. O lucro flutuante é reconciliado separadamente pelo Worker MT5.

### 📊 Resultados

Apresenta os fechamentos mais recentes das operações do copiador. A tela identifica:

- ativo e direção;
- motivo do fechamento, como TP, Stop, BE, Stop Out ou outro fechamento;
- alvo atingido, quando o encerramento ocorreu por Take Profit;
- resultado líquido da ordem.

Caso ainda não existam fechamentos conciliados, a tela pode mostrar o histórico recente de execuções ou comandos registrados.

### 📻 Canais

Controla quais salas aprovadas podem enviar sinais para a conta do usuário. Os nomes exibidos são nomes comerciais, como **Gold Alpha**, **Gold Prime** e outros; os nomes e IDs reais permanecem internos.

Opções disponíveis:

- **Seguir todos:** libera automaticamente todos os canais ativos e aprovados;
- **Escolher canais:** permite marcar individualmente as salas desejadas;
- **Sugerir novo canal:** recebe link público, convite privado, link do Telegram Web ou `@username`;
- **Como funciona:** explica o processo de análise e aprovação;
- **Atualizar:** recarrega catálogo, seleção e solicitações em análise.

Sugerir um canal não o ativa automaticamente. O administrador precisa acessá-lo, confirmar que a conta principal de monitoramento participa dele, analisar o padrão das mensagens e aprová-lo. Isso evita que formatos incompatíveis gerem operações incorretas.

### ⚙️ Configurações

Centraliza gestão de risco, proteções, execução dos sinais, alertas, notícias e contas MT5. Seus submenus são detalhados mais adiante.

### 📡 Status

Mostra a saúde dos principais componentes:

- bot de gestão;
- monitor de sinais do Telegram;
- conexão com o MetaTrader 5;
- horário da última atualização da tela.

Esse menu serve para diagnosticar rapidamente se o cliente está apenas bloqueado por configuração ou se algum serviço/conexão realmente está indisponível.

### 🛑 Parar sinais hoje

Interrompe somente a abertura de novas operações. Ordens pendentes e posições já existentes continuam sendo acompanhadas pelo Worker, inclusive proteções, BE, trailing e conciliação de fechamentos.

A parada é temporária e o sistema informa a data e o horário da retomada automática, atualmente às **23:00 no horário de Brasília**. Enquanto a pausa diária estiver ativa, o usuário também pode usar **Retomar sinais agora**.

Essa função é diferente de **Pausar conta**: a parada diária termina automaticamente; a pausa da conta permanece até a reativação.

### 🔄 Atualizar

Recarrega o painel principal e refaz a leitura dos estados armazenados. Não reinicia serviços e não envia nenhuma ordem.

## Menu Configurações

### ⚙️ Gestão de risco

Define o tamanho e os limites das operações.

#### Modo de lote

Escolhe entre:

- **Lote fixo:** usa um volume total definido pelo usuário em cada sinal;
- **Risco percentual:** calcula o lote com base na equity da conta, no percentual escolhido e na distância entre entrada e Stop Loss.

No risco percentual, o Stop Loss técnico enviado pela sala é mantido e o lote é reduzido para tentar respeitar o valor financeiro. Se nem o lote mínimo da corretora couber no limite, o sinal deve ser rejeitado por segurança.

#### Lote fixo

Oferece valores prontos de `0.01` a `1.00` e uma opção personalizada. Esse valor representa o lote total do sinal. Quando são usados vários TPs, o sistema divide o volume entre eles, respeitando o lote mínimo e o passo de volume da corretora.

#### Risco percentual

Oferece percentuais predefinidos e valor personalizado. O percentual somente controla o lote se o **Modo de lote** estiver realmente definido como **Risco percentual**. Apenas preencher `0,5%`, mantendo o modo em lote fixo, não transforma o Stop da sala em um Stop financeiro de 0,5%.

#### Meta diária

Define um objetivo financeiro diário. A proteção considera o resultado realizado e o resultado flutuante das operações do copiador. Ao alcançar a meta, o sistema protege o resultado, encerra/cancela operações sob seu controle e bloqueia novas entradas até a retomada diária.

#### Limite de perda

É o Stop financeiro diário do copiador. Ao atingir o limite negativo configurado, o sistema encerra posições, cancela pendentes e bloqueia novas entradas. Comissões, swap, spread, slippage e a velocidade de execução podem fazer o valor final variar um pouco em relação ao limite exato.

Esse limite não altera o Stop Loss técnico individual de cada sinal. Ele funciona como uma proteção financeira global do dia.

#### Máximo de operações

Limita quantos grupos de sinais podem permanecer ativos ao mesmo tempo. Um sinal dividido em vários TPs continua sendo um único grupo para essa finalidade.

#### Spread máximo

Impede uma nova entrada quando a diferença entre compra e venda estiver acima do número de pontos aceito. Protege contra entradas caras em períodos de baixa liquidez ou alta volatilidade.

#### Slippage

Define o desvio máximo, em pontos, tolerado nas execuções a mercado. Não garante preço exato, mas limita quanto a execução pode se afastar do preço solicitado conforme as regras do broker.

### 🛡️ Proteções

#### BE após TP1

Quando o primeiro alvo do sinal é confirmado, move o Stop Loss das posições restantes para o preço de entrada. O objetivo é impedir que o lucro obtido no TP1 seja seguido por uma perda completa nas demais partes da operação.

#### BE antecipado em 1R

Move a proteção para a entrada quando o mercado avança a favor uma distância equivalente ao risco inicial. Exemplo: se a distância da entrada ao Stop original é de 10 pontos, o marco de 1R ocorre após avanço favorável de aproximadamente 10 pontos.

#### Trailing Stop

Após o avanço exigido pela estratégia, acompanha o preço favorável ajustando o Stop. O trailing pode melhorar a proteção, mas nunca deve afastar o Stop e aumentar o risco novamente.

#### Configurar limite diário

É um atalho para a configuração do limite de perda financeira diária. A tela de Proteções também informa se o limite diário e a meta diária estão ativos e quais valores estão protegidos.

### 🎯 Execução dos sinais

Define como o sistema transforma um sinal validado em ordens MT5.

#### Modo de entrada

- **Entrar imediatamente:** executa ao preço disponível quando o sinal chega, mesmo fora da faixa indicada. Ainda valida SL, TPs, margem, spread e limites; se o mercado já tiver ultrapassado os alvos, a entrada pode ser rejeitada.
- **Posicionar na entrada:** cria uma ordem pendente no preço selecionado dentro da faixa e aguarda o mercado chegar.
- **Mercado somente na zona:** entra a mercado se o preço já estiver na zona; se estiver fora, posiciona a ordem para aguardar a faixa.

#### Preço da faixa

- **Primeiro toque:** utiliza o primeiro ponto da faixa que o mercado deve encontrar;
- **Meio da faixa:** usa o ponto médio entre os dois preços informados;
- **Distribuir na faixa:** distribui as entradas entre os pontos disponíveis da zona.

Essa escolha não se aplica ao modo de entrada imediata.

#### Validade

Define por quanto tempo uma ordem pendente poderá ficar esperando: 30 minutos, 1 hora, 2 horas, 4 horas ou até o fim do dia. Ao expirar, a pendente deixa de ser uma oportunidade válida e deve ser cancelada pelo Worker.

#### Quantidade de TPs

Permite usar somente TP1, TP1 e TP2, até TP3, até TP4 ou todos os alvos enviados pela sala. Se o sinal tiver menos alvos que o limite escolhido, o sistema usa apenas os existentes. O lote total é dividido entre os TPs selecionados.

### 🔔 Alertas de resultados

Permite ativar ou desativar a notificação de cada fechamento. Quando habilitado, o cliente recebe:

- motivo do fechamento, como TP, Stop ou BE;
- resultado líquido daquela ordem;
- consolidação dos resultados do dia.

Os valores incluem lucro/prejuízo, comissão, swap e taxas reportadas pelo MT5.

### 📰 Notícias do mercado

Permite escolher entre operar normalmente ou bloquear novas entradas durante notícias fortes relacionadas ao ativo.

Quando a proteção está ativa:

- novas entradas são bloqueadas na janela configurada antes e depois do evento;
- posições já abertas não são encerradas ou modificadas por essa função;
- clientes ativos recebem aviso antes e no momento da notícia.

Se o usuário nunca escolher uma opção, o padrão é permitir operações. A função depende de o calendário financeiro gratuito estar conectado e atualizado.

### 🖥️ Minhas contas MT5

Mostra a conta principal associada ao usuário, incluindo login mascarado, servidor, tipo real/demo, modo hedging/netting, saldo, equity, conexão, heartbeat do Worker e situação do copiador.

Subopções:

- **Ver conta:** retorna aos dados atuais da conta;
- **Configurar:** abre as preferências de execução dos sinais;
- **Execução dos sinais:** acesso direto ao submenu de entrada, faixa, validade e TPs;
- **Testar conexão:** tenta inicializar o terminal e validar a conta novamente;
- **Remover conta:** após confirmação, remove credencial criptografada e perfil de execução vinculados.

### 🔗 Conectar conta MT5

Abre a Mini App segura em HTTPS. O cliente informa corretora, servidor, login, senha e apelido. A lista pode oferecer servidores conhecidos e também permitir digitação manual quando o servidor não aparecer.

O processo cria uma instalação MT5 isolada para a conta, valida login, servidor, tipo de conta e permissão de negociação. Conectar a conta não significa que os sinais já estão liberados: a assinatura e a aprovação administrativa também precisam estar válidas.

### 🔐 Solicitar ativação

Explica o processo de liberação. O cliente pode conectar e configurar o MT5, mas a abertura de sinais somente é autorizada depois que o administrador registra aprovação, pagamento e validade do acesso.

### ⏸️ Pausar conta

Bloqueia novas entradas por tempo indeterminado. Não fecha automaticamente posições já abertas. Para voltar a receber sinais, o usuário utiliza a opção de reativação, sujeita às regras de assinatura e aprovação.

## Menus exclusivos do administrador

### 🛡️ Painel Admin

Abre o painel administrativo web dentro do Telegram. É a central para acompanhar e gerenciar clientes, aprovações, contas MT5, vencimentos, pagamentos, catálogo de canais e solicitações de novos canais.

### 🖥️ Acessar pelo PC

Gera um link administrativo de uso único, com validade de cinco minutos. O administrador abre esse link no navegador do computador e, após o login, mantém uma sessão de até 12 horas. O link não deve ser compartilhado.

## Estados importantes exibidos pelo bot

- **Aguardando aprovação:** cadastro existe, mas o administrador ainda não liberou o acesso;
- **Ativo / operações liberadas:** cliente aprovado, pagamento válido, conta e perfil habilitados;
- **Acesso expirado:** a validade terminou e novas entradas foram bloqueadas;
- **Pausado:** usuário interrompeu a conta até solicitar reativação;
- **Sinais parados por hoje:** bloqueio temporário até 23:00 de Brasília ou retomada manual;
- **MT5 desconectado:** terminal, login, servidor, permissão de negociação ou Worker precisam ser verificados.

## Fluxo completo de uma operação

1. O canal aprovado publica uma mensagem.
2. O monitor identifica o canal e interpreta o sinal.
3. O sistema verifica duplicidade, idade da mensagem e formato.
4. Para cada cliente, valida assinatura, status, canal escolhido e pausa diária.
5. Confere modo de entrada, notícia, spread, slippage, margem e limites de risco.
6. Calcula o lote e divide os TPs conforme a configuração.
7. Envia as ordens à instalação MT5 isolada da conta.
8. O Worker acompanha pendentes e posições, aplica BE/trailing e limites financeiros.
9. Ao fechar, reconcilia o resultado e atualiza Minha conta, Resultados e alertas.

