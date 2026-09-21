-- Registro central do portal (Instituto Trader / Robo Braba).
--
-- Espelha, de forma idempotente, o que vive nos SQLite de cada VPS para dar
-- uma fonte unica de verdade: onde cada cliente executa, qual versao roda e
-- qual o estado financeiro. O motor de execucao MT5 continua nas VPSs.
--
-- Regras desta migration:
--   * Schema proprio ("portal"); nao toca nenhuma tabela existente do projeto.
--   * Nunca guarda senha MT5 (nem hash) nem o login completo, so os 4 ultimos digitos.
--   * RLS ligado sem policies e sem GRANT para anon/authenticated: somente o
--     backend, com a service_role, le e escreve. Nao expor "portal" na Data API.
--   * Nao depende do Supabase Auth (o login do portal continua na API propria).
--
-- Rollback: drop schema portal cascade;

create schema if not exists portal;

revoke all on schema portal from public, anon, authenticated;
grant usage on schema portal to service_role;

create or replace function portal.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- VPSs / nos de execucao ------------------------------------------------------
create table portal.nodes (
  id            text primary key,
  label         text not null,
  max_terminals integer not null default 10 check (max_terminals > 0),
  app_version   text,
  git_commit    text,
  deployed_at   timestamptz,
  last_seen_at  timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- Marcas / instalacoes (uma por SQLite) --------------------------------------
create table portal.instances (
  id            text primary key check (id in ('main', 'robo_braba')),
  brand_name    text not null,
  node_id       text references portal.nodes (id) on delete set null,
  db_version    text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- Clientes ---------------------------------------------------------------------
create table portal.customers (
  id                uuid primary key default gen_random_uuid(),
  instance_id       text not null references portal.instances (id),
  source_user_id    bigint not null,
  telegram_user_id  bigint,
  status            text not null,
  customer_name     text,
  email             text,
  phone             text,
  plan_name         text,
  monthly_amount    numeric(12, 2) not null default 0,
  due_date          date,
  billing_status    text not null default 'pending',
  last_paid_at      date,
  created_at        timestamptz not null,
  updated_at        timestamptz not null default now(),
  synced_at         timestamptz not null default now(),
  unique (instance_id, source_user_id)
);
create index customers_email_idx on portal.customers (lower(email));

-- Contas MT5 (sem credenciais) ---------------------------------------------------
create table portal.mt5_accounts (
  id                   uuid primary key default gen_random_uuid(),
  customer_id          uuid not null references portal.customers (id) on delete cascade,
  instance_id          text not null references portal.instances (id),
  source_account_id    bigint not null,
  node_id              text references portal.nodes (id) on delete set null,
  broker_name          text not null,
  server_name          text not null,
  login_last4          text,
  account_alias        text,
  account_type         text not null,
  account_mode         text not null default 'hedging',
  connection_status    text not null,
  last_error           text,
  balance              numeric(18, 2),
  equity               numeric(18, 2),
  worker_heartbeat_at  timestamptz,
  created_at           timestamptz not null,
  updated_at           timestamptz not null default now(),
  synced_at            timestamptz not null default now(),
  unique (instance_id, source_account_id)
);
create index mt5_accounts_customer_idx on portal.mt5_accounts (customer_id);
create index mt5_accounts_node_idx on portal.mt5_accounts (node_id);

-- Pagamentos ---------------------------------------------------------------------
create table portal.payments (
  id                 uuid primary key default gen_random_uuid(),
  customer_id        uuid not null references portal.customers (id) on delete cascade,
  instance_id        text not null references portal.instances (id),
  source_payment_id  bigint not null,
  amount             numeric(12, 2) not null,
  paid_at            date not null,
  period_start       date,
  period_end         date,
  method             text,
  reference          text,
  status             text not null default 'paid',
  created_at         timestamptz not null,
  synced_at          timestamptz not null default now(),
  unique (instance_id, source_payment_id)
);
create index payments_customer_idx on portal.payments (customer_id);

-- Canais de sinais ---------------------------------------------------------------
create table portal.channels (
  id                 uuid primary key default gen_random_uuid(),
  instance_id        text not null references portal.instances (id),
  source_channel_id  bigint not null,
  telegram_chat_id   text,
  title              text not null,
  display_name       text,
  status             text not null,
  access_status      text not null,
  created_at         timestamptz not null,
  updated_at         timestamptz not null default now(),
  synced_at          timestamptz not null default now(),
  unique (instance_id, source_channel_id)
);

-- Auditoria das sincronizacoes VPS -> Supabase ---------------------------------
create table portal.sync_runs (
  id           bigint generated always as identity primary key,
  instance_id  text not null references portal.instances (id),
  node_id      text references portal.nodes (id) on delete set null,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  status       text not null default 'running' check (status in ('running', 'ok', 'error')),
  row_counts   jsonb not null default '{}'::jsonb,
  error        text
);
create index sync_runs_instance_idx on portal.sync_runs (instance_id, started_at desc);

-- updated_at automatico -------------------------------------------------------------
create trigger nodes_set_updated_at        before update on portal.nodes
  for each row execute function portal.set_updated_at();
create trigger instances_set_updated_at    before update on portal.instances
  for each row execute function portal.set_updated_at();
create trigger customers_set_updated_at    before update on portal.customers
  for each row execute function portal.set_updated_at();
create trigger mt5_accounts_set_updated_at before update on portal.mt5_accounts
  for each row execute function portal.set_updated_at();
create trigger channels_set_updated_at     before update on portal.channels
  for each row execute function portal.set_updated_at();

-- RLS: ligado, sem policies. Somente service_role (que ignora RLS) acessa. ------
alter table portal.nodes         enable row level security;
alter table portal.instances     enable row level security;
alter table portal.customers     enable row level security;
alter table portal.mt5_accounts  enable row level security;
alter table portal.payments      enable row level security;
alter table portal.channels      enable row level security;
alter table portal.sync_runs     enable row level security;

revoke all on all tables in schema portal from public, anon, authenticated;
revoke all on all sequences in schema portal from public, anon, authenticated;
revoke all on function portal.set_updated_at() from public, anon, authenticated;
grant all on all tables in schema portal to service_role;
grant all on all sequences in schema portal to service_role;

-- Registro inicial das duas marcas (sem VPS associada ate o primeiro sync). -----
insert into portal.instances (id, brand_name) values
  ('main', 'Instituto Trader'),
  ('robo_braba', 'Robo Braba');
