-- Estende o papel de privilegio minimo do shadow-write (portal_shadow_writer,
-- criado em 20260924010000_add_shadow_writer_role.sql) pra cobrir o mirror
-- de execucao da Etapa 2 (finalizacao): quais contas MT5 receberam cada
-- sinal e o resultado real (ticket/retcode), em
-- portal.customers/portal.mt5_accounts/portal.execution_jobs/execution_job_orders.
--
-- Mesma logica de sempre: RLS ligado sem nenhuma policy bloqueia tudo por
-- padrao, mesmo com GRANT -- por isso cada tabela nova ganha GRANT +
-- policy juntos, nunca um sem o outro.
--
-- Continua explicitamente SEM acesso: payments, sync_runs,
-- execution_attempts, account_signal_claims, channel_subscriptions,
-- rejection_codes, positions, notifications, notification_deliveries,
-- audit_events -- nada disso foi concedido, e RLS sem policy garante que
-- fica assim mesmo se um dia alguem conceder GRANT por engano sem tambem
-- criar a policy correspondente.
--
-- Rollback:
--   drop policy if exists portal_shadow_writer_customers on portal.customers;
--   drop policy if exists portal_shadow_writer_mt5_accounts on portal.mt5_accounts;
--   drop policy if exists portal_shadow_writer_execution_jobs on portal.execution_jobs;
--   drop policy if exists portal_shadow_writer_execution_job_orders on portal.execution_job_orders;
--   revoke all on portal.customers, portal.mt5_accounts, portal.execution_jobs, portal.execution_job_orders
--     from portal_shadow_writer;

grant select, insert, update on portal.customers to portal_shadow_writer;
grant select, insert, update on portal.mt5_accounts to portal_shadow_writer;
grant select, insert, update on portal.execution_jobs to portal_shadow_writer;
grant select, insert, update on portal.execution_job_orders to portal_shadow_writer;

-- for all + using(true)/with check(true) nao e um risco aqui: a policy so
-- libera o que o GRANT acima ja permitiu (select/insert/update) -- nunca
-- mais que isso. delete continua bloqueado porque nunca foi concedido.
create policy portal_shadow_writer_customers on portal.customers
  for all to portal_shadow_writer using (true) with check (true);
create policy portal_shadow_writer_mt5_accounts on portal.mt5_accounts
  for all to portal_shadow_writer using (true) with check (true);
create policy portal_shadow_writer_execution_jobs on portal.execution_jobs
  for all to portal_shadow_writer using (true) with check (true);
create policy portal_shadow_writer_execution_job_orders on portal.execution_job_orders
  for all to portal_shadow_writer using (true) with check (true);
