-- home-products-kb · 002 · fix: doc_kind 'usage_summary' broke source_id
-- source_id is built as product_id || '-' || doc_kind, and must match ^[a-z0-9]+(-[a-z0-9]+)*$.
-- The underscore in 'usage_summary' failed that check, so kb_create_version raised and n8n returned 500.
-- Fix: replace '_' with '-' when building source_id (→ '<product>-usage-summary'). Replaces one function only.

create or replace function public.kb_create_version(
  p_product_id text, p_doc_kind text, p_source_title text, p_source_url text, p_lang text default null)
returns table (upload_id uuid, source_id text, version_no int)
language plpgsql set search_path = public as $$
declare v_source text := p_product_id || '-' || replace(p_doc_kind, '_', '-'); v_no int;
begin
  if exists (select 1 from kb_versions v where v.source_id = v_source and v.status = 'uploading') then
    raise exception 'upload_in_progress';
  end if;
  select coalesce(max(v.version_no), 0) + 1 into v_no from kb_versions v where v.source_id = v_source;
  return query
    insert into kb_versions (source_id, product_id, doc_kind, source_title, source_url, lang, version_no)
    values (v_source, p_product_id, p_doc_kind, p_source_title, p_source_url, p_lang, v_no)
    returning id, kb_versions.source_id, kb_versions.version_no;
end $$;

revoke execute on function public.kb_create_version(text,text,text,text,text) from public, anon, authenticated;
grant execute on function public.kb_create_version(text,text,text,text,text) to service_role;
