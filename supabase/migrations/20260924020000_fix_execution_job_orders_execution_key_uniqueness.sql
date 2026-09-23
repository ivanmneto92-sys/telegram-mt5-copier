-- Corrige um defeito real do schema da Etapa 1 (ja aplicado):
-- portal.execution_job_orders.execution_key tinha "unique (execution_key)"
-- GLOBAL na tabela inteira, nao por conta. Mas execution_key e derivado so
-- do sinal (8 chars hex de signal.signature + indice do TP, ver
-- central_sync.build_execution_key) -- e IGUAL para toda conta que copia o
-- mesmo sinal no mesmo TP. Assim que duas contas diferentes (dois clientes,
-- ou dois assinantes do mesmo canal) executassem o mesmo sinal, a segunda
-- gravacao quebraria essa constraint -- nao e um caso raro, e o caso normal
-- do negocio (varios clientes assinando o mesmo canal).
--
-- O uso pretendido do execution_key (comentario no cabecalho de
-- 20260923120000_execution_queue.sql: o agente da VPS, na Etapa 4, confere
-- se o comentario ja existe no PROPRIO terminal MT5 antes de reenviar uma
-- ordem) nunca precisou de unicidade GLOBAL -- cada terminal MT5 e isolado
-- por conta. "unique (execution_job_id, tp_index)" (mesma migration
-- original, constraint execution_job_orders_execution_job_id_tp_index_key)
-- ja garante idempotencia por job -- isso sozinho e suficiente.
--
-- Nome da constraint confirmado ao vivo contra o Postgres local
-- (pg_constraint) antes de escrever esta migration.
--
-- Rollback (so seguro se nenhuma linha com execution_key duplicado entre
-- contas diferentes existir -- conferir antes com um SELECT execution_key,
-- COUNT(*) ... GROUP BY execution_key HAVING COUNT(*) > 1):
--   alter table portal.execution_job_orders
--     add constraint execution_job_orders_execution_key_key unique (execution_key);

alter table portal.execution_job_orders
  drop constraint execution_job_orders_execution_key_key;
