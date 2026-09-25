-- Fila central de execucao -- Etapa 1 da arquitetura final:
--   Telegram -> ingestao -> backend central -> fila -> agente de VPS -> MT5 -> portal
--
-- Puramente estrutural. Nao inclui: codigo do listener escrevendo em modo
-- sombra (Etapa 2), o agente de execucao da VPS (Etapa 4), nem a criacao dos
-- usuarios auth.users por VPS -- isso acontece quando a primeira VPS real for
-- provisionada contra esta fila.
--
-- Regras desta migration:
--   * Dois schemas: "portal" (privado, tabelas, so service_role e as funcoes
--     SECURITY DEFINER abaixo) e "agent_api" (exposto pela Data API, so
--     funcoes RPC, sem tabelas). O PostgREST so expoe funcoes de um schema
--     que esta na lista de schemas expostos da API -- por isso as RPCs
--     precisam morar em agent_api mesmo com portal continuando trancado.
--   * Toda funcao de agent_api deriva o node autenticado de auth.uid() ->
--     portal.nodes.auth_user_id DENTRO da funcao. Nunca confia num
--     node_id/agent_id enviado como parametro pelo chamador -- um agente so
--     consegue reivindicar/concluir jobs do proprio node porque a funcao
--     descobre quem ele e pelo JWT, nao pelo que ele diz que e.
--   * As funcoes de agent_api sao SECURITY DEFINER: rodam com o privilegio de
--     quem as criou (o papel da migration, que e dono das tabelas de
--     "portal" e portanto ignora RLS), nao com o privilegio de quem chama.
--     "authenticated" nunca recebe acesso direto as tabelas de portal, so
--     EXECUTE nessas funcoes especificas -- e por isso "portal" pode
--     continuar sem nenhuma policy de RLS.
--   * O MT5 continua sendo a autoridade real de posicoes e ordens. Esta fila
--     garante entrega "pelo menos uma vez" pro efeito externo (nao ha
--     transacao distribuida entre Postgres e MT5) -- o agente da VPS, na
--     Etapa 4, precisa reconciliar contra o terminal antes de reenviar
--     qualquer ordem, usando o execution_key (mesmo formato de comentario de
--     mt5/trade_comment.py, ate 26 caracteres) como prova verificavel.
--   * Nunca guarda credencial MT5.
--   * Exclusao fisica de cliente/conta e substituida por exclusao logica
--     (retired_at) a partir desta migration -- uma vez que exista job,
--     tentativa, ticket ou auditoria pendurados numa conta, apagar a linha
--     fisicamente destruiria historico operacional. FKs de historico de
--     execucao usam "on delete restrict".
--   * RLS ligado em toda tabela nova, sem policies -- mesmo padrao da
--     migration anterior.
--
-- Rollback:
--   drop schema agent_api cascade;
--   drop table portal.audit_events, portal.notification_deliveries,
--     portal.notifications, portal.positions, portal.rejection_codes,
--     portal.execution_job_orders, portal.execution_attempts,
--     portal.execution_jobs, portal.channel_subscriptions,
--     portal.account_signal_claims, portal.signal_revisions, portal.signals
--     cascade;
--   alter table portal.customers drop column retired_at;
--   alter table portal.mt5_accounts drop column retired_at;
--   alter table portal.instances drop column kill_switch_enabled,
--     add constraint instances_id_check check (id in ('main', 'robo_braba'));
--   alter table portal.nodes drop column auth_user_id, drop column
--     kill_switch_enabled, drop column status;

-- ============================================================================
-- Schema agent_api: exposto pela Data API, so funcoes RPC, sem tabelas.
-- ============================================================================
create schema if not exists agent_api;
revoke all on schema agent_api from public, anon;
grant usage on schema agent_api to authenticated;

-- ============================================================================
-- Extensoes a portal.nodes / portal.instances / portal.customers / portal.mt5_accounts
-- ============================================================================

-- Credencial revogavel por VPS (um auth.users por node, id guardado aqui) e
-- kill switch central. app_metadata.node_id no JWT e so uma checagem
-- adicional -- toda funcao sensivel sempre reconfirma auth_user_id/status
-- contra esta tabela, porque um JWT ja emitido continua valido ate expirar
-- mesmo depois do usuario ser desativado.
alter table portal.nodes
  add column auth_user_id       uuid references auth.users (id) on delete set null,
  add column kill_switch_enabled boolean not null default false,
  add column status             text not null default 'active' check (status in ('active', 'suspended'));

-- Remove o CHECK fixo em duas marcas -- bloqueava "homolog" e qualquer marca
-- futura sem necessidade; adiciona kill switch por instancia/marca.
alter table portal.instances drop constraint instances_id_check;
alter table portal.instances
  add column kill_switch_enabled boolean not null default false;

-- Exclusao logica em vez de fisica a partir de agora.
alter table portal.customers add column retired_at timestamptz;
alter table portal.mt5_accounts add column retired_at timestamptz;

-- ============================================================================
-- Sinais e revisoes
-- ============================================================================

create table portal.signals (
  id                    uuid primary key default gen_random_uuid(),
  instance_id           text not null references portal.instances (id),
  channel_id            uuid not null references portal.channels (id) on delete restrict,
  source_message_id     bigint not null,  -- id da mensagem do Telegram: ancora de revisao
  content_signature     text not null,    -- assinatura da revisao mais recente
  executed_revision_no  integer,          -- qual revisao gerou jobs de execucao; NULL = nenhuma ainda
  symbol                text,
  direction              text,
  entry_low             numeric,
  entry_high            numeric,
  stop_loss             numeric,
  take_profits          jsonb,
  status                text not null default 'active' check (status in ('active', 'closed', 'cancelled')),
  first_seen_at         timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  unique (instance_id, channel_id, source_message_id)
);
create index signals_content_signature_idx on portal.signals (content_signature);

-- Append-only. A numeracao de revisao NAO pode ser "max(revision_no)+1" direto
-- na aplicacao -- nao e seguro com escritores concorrentes. Use sempre
-- portal.append_signal_revision(), que trava a linha do sinal primeiro.
create table portal.signal_revisions (
  id                bigint generated always as identity primary key,
  signal_id         uuid not null references portal.signals (id) on delete restrict,
  revision_no       integer not null,
  content_signature text not null,
  raw_payload       jsonb not null,
  received_at       timestamptz not null default now(),
  unique (signal_id, revision_no),
  unique (signal_id, content_signature)
);
create index signal_revisions_signal_idx on portal.signal_revisions (signal_id);

-- Grava uma revisao de forma atomica: trava a linha do sinal (serializa
-- escritores concorrentes do MESMO sinal), reaproveita a revisao existente se
-- o content_signature ja foi visto (retry-safe via a unique acima), soma 1 na
-- proxima revisao só depois de segurar o lock, e atualiza o sinal.
create or replace function portal.append_signal_revision(
  p_signal_id uuid,
  p_content_signature text,
  p_raw_payload jsonb
) returns portal.signal_revisions
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_next_revision integer;
  v_existing portal.signal_revisions;
  v_row portal.signal_revisions;
begin
  perform 1 from portal.signals where id = p_signal_id for update;

  select * into v_existing from portal.signal_revisions
   where signal_id = p_signal_id and content_signature = p_content_signature;
  if found then
    return v_existing;
  end if;

  select coalesce(max(revision_no), 0) + 1 into v_next_revision
    from portal.signal_revisions where signal_id = p_signal_id;

  insert into portal.signal_revisions (signal_id, revision_no, content_signature, raw_payload)
  values (p_signal_id, v_next_revision, p_content_signature, p_raw_payload)
  returning * into v_row;

  update portal.signals
     set content_signature = p_content_signature, updated_at = now()
   where id = p_signal_id;

  return v_row;
end;
$$;
revoke all on function portal.append_signal_revision(uuid, text, jsonb) from public, anon, authenticated;
grant execute on function portal.append_signal_revision(uuid, text, jsonb) to service_role;

-- Gate efemero de deduplicacao por conteudo equivalente, janela igual a
-- DUPLICATE_WINDOW_MINUTES local (240 min) -- espelha claim_signal/has_duplicate
-- (telegram_mt5_copier/database.py), que tambem checa os sinais ja gravados
-- dentro da janela, nao so a tabela de claims.
create table portal.account_signal_claims (
  mt5_account_id    uuid not null references portal.mt5_accounts (id) on delete restrict,
  content_signature text not null,
  claimed_at        timestamptz not null default now(),
  primary key (mt5_account_id, content_signature)
);
create index account_signal_claims_claimed_at_idx on portal.account_signal_claims (claimed_at);

-- ============================================================================
-- Assinatura de canal por CLIENTE (nao por conta MT5) -- preserva o
-- comportamento atual: user_channel_subscriptions(user_id, source_channel_id)
-- no SQLite local, e client_portal.toggle_channel(user_id, channel_id) no
-- portal ja operam por usuario/cliente, nao por conta.
-- ============================================================================
create table portal.channel_subscriptions (
  id             uuid primary key default gen_random_uuid(),
  customer_id    uuid not null references portal.customers (id) on delete restrict,
  channel_id     uuid not null references portal.channels (id) on delete restrict,
  status         text not null default 'active' check (status in ('active', 'inactive')),
  risk_overrides jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (customer_id, channel_id)
);
create index channel_subscriptions_customer_idx on portal.channel_subscriptions (customer_id);
create index channel_subscriptions_channel_idx on portal.channel_subscriptions (channel_id);

-- ============================================================================
-- Fila de execucao
-- ============================================================================

create table portal.execution_jobs (
  id                   uuid primary key default gen_random_uuid(),
  instance_id          text not null references portal.instances (id),
  mt5_account_id       uuid not null references portal.mt5_accounts (id) on delete restrict,
  node_id              text not null references portal.nodes (id),
  signal_id            uuid not null references portal.signals (id) on delete restrict,
  content_signature    text not null,
  job_type             text not null default 'open' check (job_type in ('open', 'modify', 'close')),
  status               text not null default 'pending' check (
                          status in ('pending', 'reserved', 'executing', 'succeeded',
                                     'rejected', 'retry_wait', 'dead_letter', 'cancelled')
                        ),
  priority             integer not null default 100,
  attempt_count        integer not null default 0,
  max_attempts         integer not null default 5,
  available_at         timestamptz not null default now(),
  reservation_token    uuid,
  reserved_by          text references portal.nodes (id),
  reserved_at          timestamptz,
  reserved_until       timestamptz,
  started_at           timestamptz,
  finished_at          timestamptz,
  cancel_requested_at  timestamptz,
  payload              jsonb not null,  -- so instrucoes de ordem; NUNCA credencial MT5
  payload_version      integer not null default 1,
  result               jsonb,
  last_error_code      text,
  last_error_message   text,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  -- Idempotencia PERMANENTE do evento: um retry do mesmo sinal pra mesma
  -- conta nunca cria um segundo job, mesmo depois de anos. Isto e diferente
  -- (e nao substitui) o gate temporario de account_signal_claims acima, que
  -- deduplica CONTEUDO equivalente vindo de sinais/mensagens diferentes
  -- dentro da janela de 240 min.
  unique (mt5_account_id, signal_id)
);

-- Indice parcial: so os estados que o claim (FOR UPDATE SKIP LOCKED) precisa
-- varrer. reserved_until entra tambem pra achar leases vencidos rapido.
create index execution_jobs_claimable_idx
  on portal.execution_jobs (node_id, available_at, created_at)
  where status in ('pending', 'reserved', 'retry_wait');
create index execution_jobs_reserved_until_idx
  on portal.execution_jobs (reserved_until) where reserved_until is not null;
create index execution_jobs_signal_idx on portal.execution_jobs (signal_id);
create index execution_jobs_account_idx on portal.execution_jobs (mt5_account_id);

-- Log append-only de cada tentativa do job (nivel job, nao nivel ordem/TP --
-- ver execution_job_orders abaixo pra isso).
create table portal.execution_attempts (
  id                 bigint generated always as identity primary key,
  execution_job_id   uuid not null references portal.execution_jobs (id) on delete restrict,
  attempt_no         integer not null,
  node_id            text not null references portal.nodes (id),
  reservation_token  uuid not null,
  started_at         timestamptz not null default now(),
  finished_at        timestamptz,
  outcome            text check (outcome in ('succeeded', 'rejected', 'failed', 'timeout')),
  rejection_code     text,
  rejection_message  text,
  raw_response       jsonb,
  unique (execution_job_id, attempt_no)
);
create index execution_attempts_job_idx on portal.execution_attempts (execution_job_id);

-- Um sinal vira varias ordens (uma por TP) -- tabela filha, uma linha por TP.
-- execution_key reaproveita o MESMO formato de comentario MT5 que
-- mt5/trade_comment.py ja usa hoje (8 chars hex do content_signature + indice
-- do TP, ate 26 caracteres) como prova de execucao verificavel: o agente da
-- VPS (Etapa 4) confere se esse execution_key ja existe no terminal antes de
-- reenviar qualquer ordem.
create table portal.execution_job_orders (
  id                    uuid primary key default gen_random_uuid(),
  execution_job_id      uuid not null references portal.execution_jobs (id) on delete restrict,
  tp_index              integer not null,
  execution_key         text not null,
  requested_volume      numeric not null,
  normalized_volume     numeric,
  entry_price           numeric,
  stop_loss             numeric,
  take_profit           numeric,
  status                text not null default 'pending' check (
                           status in ('pending', 'sent', 'filled', 'cancelled', 'failed', 'closed')
                         ),
  mt5_order_ticket      bigint,
  mt5_position_ticket   bigint,
  retcode               integer,
  retcode_message       text,
  rejection_code        text,  -- texto livre, ver portal.rejection_codes abaixo (sem FK rigida)
  sent_at               timestamptz,
  filled_at             timestamptz,
  cancelled_at          timestamptz,
  closed_at             timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  unique (execution_job_id, tp_index),
  unique (execution_key)
);
create index execution_job_orders_job_idx on portal.execution_job_orders (execution_job_id);
create index execution_job_orders_position_ticket_idx on portal.execution_job_orders (mt5_position_ticket);

-- Catalogo dos codigos de rejeicao conhecidos (hoje definidos em
-- rejection_reason_label(), mt5/pending_order_executor.py), semeado abaixo.
-- Sem FK rigida a partir de execution_job_orders/execution_attempts: existem
-- codigos dinamicos ("order_send_failed:<detalhe>") e uma versao nova do
-- agente pode reportar um codigo antes da migration que adiciona o rotulo
-- correspondente -- FK obrigatoria quebraria o registro do erro bem na hora
-- que mais precisa funcionar. O lookup do rotulo cai pro proprio codigo como
-- fallback quando nao encontrado aqui.
create table portal.rejection_codes (
  code       text primary key,
  label_pt   text not null,
  category   text,
  created_at timestamptz not null default now()
);

-- ============================================================================
-- Posicoes: projecao OBSERVADA do MT5, que continua sendo a autoridade real.
-- Em conta netting uma posicao pode agregar ordens de mais de um job -- por
-- isso a ligacao e via execution_job_orders.mt5_position_ticket (que pode
-- aparecer em varias linhas), nao uma FK unica e rigida aqui.
-- ============================================================================
create table portal.positions (
  id                uuid primary key default gen_random_uuid(),
  instance_id       text not null references portal.instances (id),
  mt5_account_id    uuid not null references portal.mt5_accounts (id) on delete restrict,
  mt5_ticket        bigint not null,
  symbol            text,
  direction          text,
  volume            numeric,
  open_price        numeric,
  stop_loss         numeric,
  take_profit       numeric,
  opened_at         timestamptz,
  closed_at         timestamptz,
  close_price       numeric,
  pnl               numeric(18, 2),
  status            text not null default 'open' check (status in ('open', 'closed')),
  last_observed_at  timestamptz not null default now(),
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (mt5_account_id, mt5_ticket)
);
create index positions_account_idx on portal.positions (mt5_account_id);

-- ============================================================================
-- Notificacoes: conteudo separado de entrega -- uma notificacao pode estar
-- entregue por e-mail, falhada no Telegram e ainda nao lida no portal ao
-- mesmo tempo; um unico campo "status" misturaria essas tres coisas.
-- ============================================================================
create table portal.notifications (
  id           uuid primary key default gen_random_uuid(),
  instance_id  text not null references portal.instances (id),
  customer_id  uuid not null references portal.customers (id) on delete restrict,
  type         text not null,
  payload      jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now(),
  read_at      timestamptz
);
create index notifications_customer_idx on portal.notifications (customer_id);

create table portal.notification_deliveries (
  id               bigint generated always as identity primary key,
  notification_id  uuid not null references portal.notifications (id) on delete cascade,
  channel          text not null check (channel in ('portal', 'email', 'telegram', 'push')),
  status           text not null default 'pending' check (status in ('pending', 'sent', 'failed')),
  attempt_count    integer not null default 0,
  sent_at          timestamptz,
  last_error       text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (notification_id, channel)
);

-- ============================================================================
-- Auditoria de configuracao/estado alterado pelo cliente ou admin (pausar,
-- trocar risco, ativar canal). Proposito diferente de execution_attempts
-- (fila) e de portal.sync_runs, que ja existe e e especifico de ETL.
-- ============================================================================
create table portal.audit_events (
  id           bigint generated always as identity primary key,
  instance_id  text not null references portal.instances (id),
  actor_type   text not null check (actor_type in ('customer', 'admin', 'system', 'vps_agent')),
  actor_id     text,
  event_type   text not null,
  entity_type  text not null,
  entity_id    text not null,
  payload      jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now()
);
create index audit_events_entity_idx on portal.audit_events (entity_type, entity_id);
create index audit_events_instance_idx on portal.audit_events (instance_id, created_at desc);

-- ============================================================================
-- updated_at automatico (reaproveita portal.set_updated_at() da migration anterior)
-- ============================================================================
create trigger signals_set_updated_at before update on portal.signals
  for each row execute function portal.set_updated_at();
create trigger channel_subscriptions_set_updated_at before update on portal.channel_subscriptions
  for each row execute function portal.set_updated_at();
create trigger execution_jobs_set_updated_at before update on portal.execution_jobs
  for each row execute function portal.set_updated_at();
create trigger execution_job_orders_set_updated_at before update on portal.execution_job_orders
  for each row execute function portal.set_updated_at();
create trigger positions_set_updated_at before update on portal.positions
  for each row execute function portal.set_updated_at();
create trigger notification_deliveries_set_updated_at before update on portal.notification_deliveries
  for each row execute function portal.set_updated_at();

-- ============================================================================
-- RLS: ligado em toda tabela nova, sem policies -- mesmo padrao da migration
-- anterior. Ninguem alem de service_role e das funcoes SECURITY DEFINER
-- abaixo toca essas tabelas.
-- ============================================================================
alter table portal.signals                 enable row level security;
alter table portal.signal_revisions        enable row level security;
alter table portal.account_signal_claims   enable row level security;
alter table portal.channel_subscriptions   enable row level security;
alter table portal.execution_jobs          enable row level security;
alter table portal.execution_attempts      enable row level security;
alter table portal.execution_job_orders    enable row level security;
alter table portal.rejection_codes         enable row level security;
alter table portal.positions               enable row level security;
alter table portal.notifications           enable row level security;
alter table portal.notification_deliveries enable row level security;
alter table portal.audit_events            enable row level security;

revoke all on all tables in schema portal from public, anon, authenticated;
revoke all on all sequences in schema portal from public, anon, authenticated;
grant all on all tables in schema portal to service_role;
grant all on all sequences in schema portal to service_role;

-- ============================================================================
-- RPCs de agente de VPS (schema agent_api, exposto). Todas SECURITY DEFINER,
-- search_path fixo, e todas derivam o node autenticado de auth.uid() no
-- inicio da funcao -- nenhum parametro de node/agent_id enviado pelo
-- chamador tem qualquer peso na autorizacao.
-- ============================================================================

-- Reivindica ate p_limit jobs pendentes/expirados do PROPRIO node (nunca de
-- outro), usando FOR UPDATE SKIP LOCKED -- padrao idiomatico do Postgres pra
-- fila de trabalho com multiplos consumidores concorrentes. Retorna um
-- reservation_token novo por linha reivindicada; toda RPC de mutacao
-- seguinte exige esse token e rejeita se nao bater, entao um agente com lease
-- vencido/transferido nunca sobrescreve o resultado de outro.
create or replace function agent_api.claim_execution_jobs(
  p_limit integer default 1,
  p_lease_seconds integer default 60
) returns setof portal.execution_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_node_id text;
  v_limit integer := least(greatest(p_limit, 1), 20);
  v_lease integer := least(greatest(p_lease_seconds, 5), 600);
begin
  select n.id into v_node_id
    from portal.nodes n
   where n.auth_user_id = auth.uid()
     and n.status = 'active'
     and n.kill_switch_enabled = false;

  if v_node_id is null then
    raise exception 'node nao autorizado ou inativo' using errcode = '28000';
  end if;

  return query
  with claimable as (
    select ej2.id
      from portal.execution_jobs ej2
      join portal.instances i on i.id = ej2.instance_id
     where ej2.node_id = v_node_id
       and i.kill_switch_enabled = false
       and ej2.attempt_count < ej2.max_attempts
       and (
             ej2.status = 'pending'
          or (ej2.status = 'reserved' and ej2.reserved_until < now())
          or (ej2.status = 'retry_wait' and ej2.available_at <= now())
       )
     order by ej2.priority, ej2.available_at, ej2.created_at, ej2.id
     for update of ej2 skip locked
     limit v_limit
  )
  update portal.execution_jobs ej
     set status = 'reserved',
         reservation_token = gen_random_uuid(),
         reserved_by = v_node_id,
         reserved_at = now(),
         reserved_until = now() + make_interval(secs => v_lease),
         attempt_count = ej.attempt_count + 1,
         updated_at = now()
    from claimable
   where ej.id = claimable.id
  returning ej.*;
end;
$$;
revoke all on function agent_api.claim_execution_jobs(integer, integer) from public, anon;
grant execute on function agent_api.claim_execution_jobs(integer, integer) to authenticated;

-- Marca um job reservado como "em execucao" -- exige o reservation_token da
-- reivindicacao.
create or replace function agent_api.start_execution_job(
  p_job_id uuid,
  p_reservation_token uuid
) returns portal.execution_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_node_id text;
  v_row portal.execution_jobs;
begin
  select n.id into v_node_id from portal.nodes n
   where n.auth_user_id = auth.uid() and n.status = 'active' and n.kill_switch_enabled = false;
  if v_node_id is null then
    raise exception 'node nao autorizado ou inativo' using errcode = '28000';
  end if;

  update portal.execution_jobs
     set status = 'executing', started_at = now(), updated_at = now()
   where id = p_job_id
     and node_id = v_node_id
     and reserved_by = v_node_id
     and reservation_token = p_reservation_token
     and status = 'reserved'
     and reserved_until > now()
  returning * into v_row;

  if v_row.id is null then
    raise exception 'job nao encontrado, expirado, ou token de reserva invalido' using errcode = 'P0002';
  end if;

  return v_row;
end;
$$;
revoke all on function agent_api.start_execution_job(uuid, uuid) from public, anon;
grant execute on function agent_api.start_execution_job(uuid, uuid) to authenticated;

-- Renova o lease de um job demorado, mesma checagem de token.
create or replace function agent_api.renew_execution_lease(
  p_job_id uuid,
  p_reservation_token uuid,
  p_lease_seconds integer default 60
) returns portal.execution_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_node_id text;
  v_row portal.execution_jobs;
  v_lease integer := least(greatest(p_lease_seconds, 5), 600);
begin
  select n.id into v_node_id from portal.nodes n
   where n.auth_user_id = auth.uid() and n.status = 'active' and n.kill_switch_enabled = false;
  if v_node_id is null then
    raise exception 'node nao autorizado ou inativo' using errcode = '28000';
  end if;

  update portal.execution_jobs
     set reserved_until = now() + make_interval(secs => v_lease), updated_at = now()
   where id = p_job_id
     and node_id = v_node_id
     and reserved_by = v_node_id
     and reservation_token = p_reservation_token
     and status in ('reserved', 'executing')
  returning * into v_row;

  if v_row.id is null then
    raise exception 'job nao encontrado ou token de reserva invalido' using errcode = 'P0002';
  end if;

  return v_row;
end;
$$;
revoke all on function agent_api.renew_execution_lease(uuid, uuid, integer) from public, anon;
grant execute on function agent_api.renew_execution_lease(uuid, uuid, integer) to authenticated;

-- Conclui um job (succeeded/rejected), grava a tentativa e faz upsert das
-- ordens por TP em execution_job_orders. p_orders e um array jsonb, um objeto
-- por TP -- o agente reporta o ticket assim que o MT5 responde, nao no fim de
-- tudo (ver comentario no cabecalho sobre gravar o ticket imediatamente).
create or replace function agent_api.complete_execution_job(
  p_job_id uuid,
  p_reservation_token uuid,
  p_status text,
  p_result jsonb default '{}'::jsonb,
  p_orders jsonb default '[]'::jsonb
) returns portal.execution_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_node_id text;
  v_row portal.execution_jobs;
  v_order jsonb;
begin
  if p_status not in ('succeeded', 'rejected') then
    raise exception 'status invalido para conclusao: %', p_status using errcode = '22023';
  end if;

  select n.id into v_node_id from portal.nodes n
   where n.auth_user_id = auth.uid() and n.status = 'active' and n.kill_switch_enabled = false;
  if v_node_id is null then
    raise exception 'node nao autorizado ou inativo' using errcode = '28000';
  end if;

  update portal.execution_jobs
     set status = p_status,
         result = p_result,
         finished_at = now(),
         updated_at = now(),
         last_error_code = case when p_status = 'rejected' then p_result ->> 'rejection_code' else null end
   where id = p_job_id
     and node_id = v_node_id
     and reserved_by = v_node_id
     and reservation_token = p_reservation_token
     and status in ('reserved', 'executing')
  returning * into v_row;

  if v_row.id is null then
    raise exception 'job nao encontrado, ja concluido, ou token de reserva invalido' using errcode = 'P0002';
  end if;

  insert into portal.execution_attempts (
    execution_job_id, attempt_no, node_id, reservation_token, started_at, finished_at, outcome
  ) values (
    v_row.id, v_row.attempt_count, v_node_id, p_reservation_token,
    coalesce(v_row.started_at, v_row.reserved_at, now()), now(), p_status
  );

  for v_order in select * from jsonb_array_elements(p_orders)
  loop
    insert into portal.execution_job_orders (
      execution_job_id, tp_index, execution_key, requested_volume, normalized_volume,
      entry_price, stop_loss, take_profit, status, mt5_order_ticket, mt5_position_ticket,
      retcode, retcode_message, rejection_code, sent_at, filled_at
    ) values (
      v_row.id,
      (v_order ->> 'tp_index')::integer,
      v_order ->> 'execution_key',
      (v_order ->> 'requested_volume')::numeric,
      (v_order ->> 'normalized_volume')::numeric,
      (v_order ->> 'entry_price')::numeric,
      (v_order ->> 'stop_loss')::numeric,
      (v_order ->> 'take_profit')::numeric,
      coalesce(v_order ->> 'status', 'sent'),
      (v_order ->> 'mt5_order_ticket')::bigint,
      (v_order ->> 'mt5_position_ticket')::bigint,
      (v_order ->> 'retcode')::integer,
      v_order ->> 'retcode_message',
      v_order ->> 'rejection_code',
      now(),
      case when (v_order ->> 'status') = 'filled' then now() else null end
    )
    on conflict (execution_job_id, tp_index) do update
       set status = excluded.status,
           mt5_order_ticket = coalesce(excluded.mt5_order_ticket, portal.execution_job_orders.mt5_order_ticket),
           mt5_position_ticket = coalesce(excluded.mt5_position_ticket, portal.execution_job_orders.mt5_position_ticket),
           retcode = excluded.retcode,
           retcode_message = excluded.retcode_message,
           rejection_code = excluded.rejection_code,
           updated_at = now();
  end loop;

  return v_row;
end;
$$;
revoke all on function agent_api.complete_execution_job(uuid, uuid, text, jsonb, jsonb) from public, anon;
grant execute on function agent_api.complete_execution_job(uuid, uuid, text, jsonb, jsonb) to authenticated;

-- Registra falha TECNICA (nao rejeicao de regra de negocio -- essa vai por
-- complete_execution_job com status='rejected'). Decide retry_wait vs
-- dead_letter aqui, com base em attempt_count/max_attempts -- e o unico lugar
-- que faz essa transicao, pra nao duplicar a logica em dois pontos.
create or replace function agent_api.fail_execution_job(
  p_job_id uuid,
  p_reservation_token uuid,
  p_error_code text,
  p_error_message text default null,
  p_retry_delay_seconds integer default 30
) returns portal.execution_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_node_id text;
  v_row portal.execution_jobs;
  v_next_status text;
begin
  select n.id into v_node_id from portal.nodes n
   where n.auth_user_id = auth.uid() and n.status = 'active' and n.kill_switch_enabled = false;
  if v_node_id is null then
    raise exception 'node nao autorizado ou inativo' using errcode = '28000';
  end if;

  select * into v_row from portal.execution_jobs
   where id = p_job_id
     and node_id = v_node_id
     and reserved_by = v_node_id
     and reservation_token = p_reservation_token
     and status in ('reserved', 'executing')
   for update;

  if v_row.id is null then
    raise exception 'job nao encontrado, ja concluido, ou token de reserva invalido' using errcode = 'P0002';
  end if;

  v_next_status := case
    when v_row.attempt_count >= v_row.max_attempts then 'dead_letter'
    else 'retry_wait'
  end;

  update portal.execution_jobs
     set status = v_next_status,
         available_at = now() + make_interval(secs => greatest(p_retry_delay_seconds, 5)),
         reserved_by = null,
         reservation_token = null,
         reserved_at = null,
         reserved_until = null,
         last_error_code = p_error_code,
         last_error_message = p_error_message,
         finished_at = case when v_next_status = 'dead_letter' then now() else null end,
         updated_at = now()
   where id = v_row.id
  returning * into v_row;

  insert into portal.execution_attempts (
    execution_job_id, attempt_no, node_id, reservation_token, started_at, finished_at, outcome,
    rejection_code, rejection_message
  ) values (
    v_row.id, v_row.attempt_count, v_node_id, p_reservation_token,
    coalesce(v_row.started_at, v_row.reserved_at, now()), now(), 'failed',
    p_error_code, p_error_message
  );

  return v_row;
end;
$$;
revoke all on function agent_api.fail_execution_job(uuid, uuid, text, text, integer) from public, anon;
grant execute on function agent_api.fail_execution_job(uuid, uuid, text, text, integer) to authenticated;

-- ============================================================================
-- Semente dos codigos de rejeicao conhecidos hoje (rejection_reason_label(),
-- mt5/pending_order_executor.py) -- fica em upsert pra novas migrations
-- poderem adicionar codigo sem conflito.
-- ============================================================================
insert into portal.rejection_codes (code, label_pt, category) values
  ('live_accounts_not_allowed', 'Execução em contas reais está desativada nesta VPS.', 'eligibility'),
  ('terminal_not_provisioned', 'O terminal MT5 desta conta ainda não foi provisionado.', 'eligibility'),
  ('account_disconnected', 'A conta MT5 está desconectada.', 'eligibility'),
  ('kill_switch_enabled', 'Execução de ordens pausada globalmente (kill switch).', 'eligibility'),
  ('real_account_blocked', 'Contas reais não são permitidas neste modo de execução.', 'eligibility'),
  ('only_demo_accounts_supported', 'Somente contas demo são suportadas neste modo.', 'eligibility'),
  ('terminal_disconnected', 'O terminal MT5 perdeu a conexão.', 'eligibility'),
  ('terminal_trading_not_allowed', 'Negociação não permitida no terminal (verifique o AutoTrading).', 'broker'),
  ('account_trading_not_allowed', 'Esta conta não tem permissão para negociar nesta corretora.', 'broker'),
  ('mt5_login_mismatch', 'O login conectado no terminal não confere com o da conta cadastrada.', 'eligibility'),
  ('symbol_select_failed', 'Não foi possível selecionar o ativo no MetaTrader 5.', 'broker'),
  ('mt5_initialize_failed', 'Falha ao iniciar o terminal MetaTrader 5.', 'eligibility'),
  ('netting_multiple_tps_not_supported', 'Conta netting não suporta múltiplos alvos (TPs) neste sinal.', 'eligibility'),
  ('missing_orders', 'Nenhuma ordem foi gerada para este sinal.', 'eligibility'),
  ('symbol_trade_disabled', 'Negociação deste ativo está desabilitada na corretora.', 'broker'),
  ('order_expired', 'A ordem pendente expirou antes de ser preenchida.', 'market'),
  ('price_hit_sl_before_entry', 'O preço já atingiu o stop antes da entrada ser alcançada.', 'market'),
  ('price_hit_tp_before_entry', 'O preço já atingiu o alvo antes da entrada ser alcançada.', 'market'),
  ('buy_stop_loss_not_below_entry', 'Stop loss inválido: precisa ficar abaixo da entrada numa compra.', 'risk'),
  ('buy_take_profit_not_above_entry', 'Take profit inválido: precisa ficar acima da entrada numa compra.', 'risk'),
  ('sell_stop_loss_not_above_entry', 'Stop loss inválido: precisa ficar acima da entrada numa venda.', 'risk'),
  ('sell_take_profit_not_below_entry', 'Take profit inválido: precisa ficar abaixo da entrada numa venda.', 'risk'),
  ('stop_loss_inside_broker_stops_level', 'Stop loss muito próximo do preço para o mínimo da corretora.', 'broker'),
  ('take_profit_inside_broker_stops_level', 'Take profit muito próximo do preço para o mínimo da corretora.', 'broker'),
  ('symbol_point_invalid', 'A corretora retornou dados inválidos para o ativo.', 'broker'),
  ('negative_spread', 'Spread inválido (negativo) reportado pela corretora.', 'broker'),
  ('max_spread_exceeded', 'Spread acima do limite máximo configurado.', 'risk'),
  ('max_open_signals_reached', 'Limite de operações simultâneas já foi atingido.', 'risk'),
  ('daily_profit_target_reached', 'Meta de lucro diária já foi atingida — novos sinais ficam bloqueados até a próxima sessão.', 'risk'),
  ('daily_loss_limit_reached', 'Limite de perda diária já foi atingido — novos sinais ficam bloqueados até a próxima sessão.', 'risk'),
  ('high_impact_news_window', 'Bloqueado pela proteção de notícias de alto impacto.', 'risk')
on conflict (code) do update set label_pt = excluded.label_pt, category = excluded.category;
