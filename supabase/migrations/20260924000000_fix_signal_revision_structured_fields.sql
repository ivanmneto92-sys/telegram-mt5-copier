-- Corrige portal.append_signal_revision(): a versao da Etapa 1
-- (20260923120000_execution_queue.sql) so atualizava content_signature em
-- portal.signals numa revisao nova, nunca os campos estruturados
-- (symbol/direction/entry_low/entry_high/stop_loss/take_profits) -- a linha
-- "atual" de portal.signals ficaria com dado estruturado desatualizado se
-- algum dia uma segunda revisao de conteudo diferente chegasse pro mesmo
-- sinal.
--
-- Inofensivo ate agora porque o listener local nunca reprocessa uma mensagem
-- ja aceita (edicao em sinal ja aceito e sempre ignorada, ver Etapa 2), entao
-- append_signal_revision na pratica so roda com revision_no=1. Mas precisa
-- estar certo antes do Supabase virar autoridade central (Etapa 4+).
--
-- p_raw_payload agora e esperado como o payload completo do shadow-write
-- (inclui os campos estruturados, nao so raw_text/formatted_message) --
-- central_sync.py:_drain_one foi ajustado nesta mesma mudanca pra passar o
-- payload completo.
--
-- Rollback: reaplicar a definicao anterior da funcao (so content_signature).

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
     set content_signature = p_content_signature,
         symbol = coalesce(p_raw_payload ->> 'symbol', symbol),
         direction = coalesce(p_raw_payload ->> 'direction', direction),
         entry_low = coalesce((p_raw_payload ->> 'entry_low')::numeric, entry_low),
         entry_high = coalesce((p_raw_payload ->> 'entry_high')::numeric, entry_high),
         stop_loss = coalesce((p_raw_payload ->> 'stop_loss')::numeric, stop_loss),
         take_profits = coalesce(p_raw_payload -> 'take_profits', take_profits),
         updated_at = now()
   where id = p_signal_id;

  return v_row;
end;
$$;
