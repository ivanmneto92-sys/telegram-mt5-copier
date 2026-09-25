-- Papel de privilegio minimo pro shadow-write da VPS (Etapa 3.5) -- nao usa
-- service_role (bypassa RLS no projeto inteiro, inclusive sistemas sem
-- relacao com este, como capital_audit_*) nem postgres (equivalente a admin).
--
-- IMPORTANTE: portal.nodes/instances/channels/signals tem RLS ligado e ZERO
-- policies (migrations anteriores). Isso significa que nenhuma linha e
-- visivel/gravavel por nenhum papel que nao seja dono/service_role, mesmo
-- com GRANT SELECT/INSERT/UPDATE -- RLS habilitado sem nenhuma policy bloqueia
-- tudo por padrao. Por isso este migration cria policies explicitas TO
-- portal_shadow_writer nas 4 tabelas -- sem elas o GRANT abaixo nao adianta
-- nada.
--
-- Dois papeis, de proposito:
--   * portal_shadow_writer: papel de CAPACIDADE, nologin -- so grants e
--     policies, nunca loga sozinho. E aqui que fica toda a logica de acesso,
--     versionada e revisavel.
--   * central_sync_vps: papel de LOGIN de verdade, membro do de capacidade
--     (herda tudo automaticamente). Criado SEM SENHA por esta migration --
--     nenhuma senha e gerada, vista ou gravada por esta sessao. A senha e
--     definida depois, direto no painel do Supabase (Database > Roles),
--     fora deste repositorio e desta conversa.
--
-- Rollback:
--   drop role if exists central_sync_vps;
--   drop policy if exists portal_shadow_writer_nodes on portal.nodes;
--   drop policy if exists portal_shadow_writer_instances on portal.instances;
--   drop policy if exists portal_shadow_writer_channels on portal.channels;
--   drop policy if exists portal_shadow_writer_signals on portal.signals;
--   revoke all on portal.nodes, portal.instances, portal.channels, portal.signals from portal_shadow_writer;
--   revoke execute on function portal.append_signal_revision(uuid, text, jsonb) from portal_shadow_writer;
--   revoke usage on schema portal from portal_shadow_writer;
--   drop role if exists portal_shadow_writer;

create role portal_shadow_writer nologin;

grant usage on schema portal to portal_shadow_writer;

grant select, insert, update on portal.nodes to portal_shadow_writer;
grant select, insert, update on portal.instances to portal_shadow_writer;
grant select, insert, update on portal.channels to portal_shadow_writer;
grant select, insert, update on portal.signals to portal_shadow_writer;

-- for all + using(true)/with check(true) nao e um risco aqui: a policy so
-- libera o que o GRANT acima ja permitiu (select/insert/update) -- nunca
-- mais que isso. delete continua bloqueado porque nunca foi concedido.
create policy portal_shadow_writer_nodes on portal.nodes
  for all to portal_shadow_writer using (true) with check (true);
create policy portal_shadow_writer_instances on portal.instances
  for all to portal_shadow_writer using (true) with check (true);
create policy portal_shadow_writer_channels on portal.channels
  for all to portal_shadow_writer using (true) with check (true);
create policy portal_shadow_writer_signals on portal.signals
  for all to portal_shadow_writer using (true) with check (true);

-- signal_revisions e SECURITY DEFINER -- so precisa de EXECUTE na funcao,
-- nunca de grant direto na tabela.
grant execute on function portal.append_signal_revision(uuid, text, jsonb) to portal_shadow_writer;

-- Explicitamente sem acesso: customers, mt5_accounts, payments, sync_runs,
-- execution_jobs, execution_attempts, execution_job_orders,
-- account_signal_claims, channel_subscriptions, rejection_codes, positions,
-- notifications, notification_deliveries, audit_events -- nada disso foi
-- concedido acima, e RLS sem policy garante que fica assim mesmo se alguem
-- um dia conceder GRANT por engano numa dessas tabelas sem tambem criar a
-- policy correspondente.

create role central_sync_vps login in role portal_shadow_writer;
