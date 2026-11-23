-- home-products-kb · 001 · products vector store schema
-- Run once in the Supabase SQL Editor of the dedicated project (empty project; creates only, drops nothing).
-- Pattern: versions + atomic swap (as in the "Alon" workflow), retrieval limited to active versions.
-- Access: RLS on, no policies; functions callable by service_role (n8n) only.

create extension if not exists vector with schema extensions;

-- One row per ingest attempt of one source document (product_id + doc_kind).
create table public.kb_versions (
  id uuid primary key default gen_random_uuid(),
  source_id text not null check (source_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  product_id text not null check (product_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  doc_kind text not null check (doc_kind in ('manual','warranty','safety','usage_summary')),
  source_title text not null,
  source_url text not null check (source_url ~ '^https://(drive|docs)\.google\.com/'),
  lang text,
  version_no int not null,
  status text not null default 'uploading' check (status in ('uploading','active','superseded','failed')),
  stage text not null default 'queued',
  pages int,
  expected_chunks int,
  error text,
  created_at timestamptz not null default now(),
  activated_at timestamptz,
  unique (source_id, version_no)
);
create unique index kb_versions_one_active on public.kb_versions (source_id) where status = 'active';
create unique index kb_versions_one_uploading on public.kb_versions (source_id) where status = 'uploading';

-- Chunks. n8n's Supabase Vector Store node writes content, metadata, embedding;
-- version_id is filled from metadata by a trigger.
create table public.kb_products (
  id bigserial primary key,
  content text not null,
  metadata jsonb not null default '{}'::jsonb,
  embedding extensions.vector(1536),
  version_id uuid references public.kb_versions(id) on delete cascade
);
create index kb_products_version_idx on public.kb_products (version_id);
create index kb_products_metadata_idx on public.kb_products using gin (metadata);
create index kb_products_embedding_idx on public.kb_products using hnsw (embedding extensions.vector_cosine_ops);

create or replace function public.kb_set_version_id() returns trigger
language plpgsql set search_path = public as $$
begin
  new.version_id := nullif(new.metadata->>'version_id','')::uuid;
  if new.version_id is null then
    raise exception 'metadata.version_id is required';
  end if;
  return new;
end $$;
create trigger kb_products_set_version before insert on public.kb_products
  for each row execute function public.kb_set_version_id();

alter table public.kb_versions enable row level security;
alter table public.kb_products enable row level security;

-- Start a new version. Raises 'upload_in_progress' (n8n maps to 409) if one is running.
create or replace function public.kb_create_version(
  p_product_id text, p_doc_kind text, p_source_title text, p_source_url text, p_lang text default null)
returns table (upload_id uuid, source_id text, version_no int)
language plpgsql set search_path = public as $$
declare v_source text := p_product_id || '-' || p_doc_kind; v_no int;
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

create or replace function public.kb_report_stage(p_upload_id uuid, p_stage text, p_pages int default null, p_expected_chunks int default null)
returns void language sql set search_path = public as $$
  update kb_versions set stage = p_stage,
    pages = coalesce(p_pages, pages), expected_chunks = coalesce(p_expected_chunks, expected_chunks)
  where id = p_upload_id and status = 'uploading';
$$;

-- Check that every expected chunk landed with a full-size vector.
create or replace function public.kb_verify_version(p_upload_id uuid)
returns table (ok boolean, expected int, stored int, bad_vectors int)
language sql set search_path = public, extensions as $$
  select (count(c.id) = v.expected_chunks and count(c.id) > 0
          and count(*) filter (where c.embedding is null or extensions.vector_dims(c.embedding) <> 1536) = 0),
         v.expected_chunks, count(c.id)::int,
         (count(*) filter (where c.id is not null and (c.embedding is null or extensions.vector_dims(c.embedding) <> 1536)))::int
  from kb_versions v left join kb_products c on c.version_id = v.id
  where v.id = p_upload_id group by v.id, v.expected_chunks;
$$;

-- Atomic swap: new version active, old one superseded and its chunks removed.
create or replace function public.kb_activate_version(p_upload_id uuid)
returns table (source_id text, version_no int, chunks int)
language plpgsql set search_path = public as $$
declare v kb_versions;
begin
  select * into v from kb_versions where id = p_upload_id and status = 'uploading' for update;
  if not found then raise exception 'not_uploading'; end if;
  if not (select ok from kb_verify_version(p_upload_id)) then raise exception 'verify_failed'; end if;
  delete from kb_products c using kb_versions o
    where c.version_id = o.id and o.source_id = v.source_id and o.status = 'active';
  update kb_versions set status = 'superseded' where kb_versions.source_id = v.source_id and status = 'active';
  update kb_versions set status = 'active', stage = 'active', activated_at = now() where id = p_upload_id;
  return query select v.source_id, v.version_no, (select count(*)::int from kb_products where version_id = p_upload_id);
end $$;

create or replace function public.kb_fail_version(p_upload_id uuid, p_error text)
returns void language plpgsql set search_path = public as $$
begin
  delete from kb_products where version_id = p_upload_id;
  update kb_versions set status = 'failed', stage = 'failed', error = left(p_error, 500)
    where id = p_upload_id and status = 'uploading';
end $$;

-- Retrieval for n8n's vector store node: (query_embedding, match_count, filter). Active versions only.
create or replace function public.match_kb_products(query_embedding extensions.vector(1536), match_count int default 6, filter jsonb default '{}'::jsonb)
returns table (id bigint, content text, metadata jsonb, similarity float)
language sql stable set search_path = public, extensions as $$
  select c.id, c.content, c.metadata, 1 - (c.embedding <=> query_embedding) as similarity
  from kb_products c join kb_versions v on v.id = c.version_id and v.status = 'active'
  where c.metadata @> coalesce(filter, '{}'::jsonb)
  order by c.embedding <=> query_embedding
  limit least(greatest(match_count, 1), 20);
$$;

create or replace function public.kb_status(p_upload_id uuid)
returns table (status text, stage text, pages int, chunks int, error text, source_id text, version_no int)
language sql stable set search_path = public as $$
  select case when v.status = 'uploading' then v.stage else v.status end, v.stage, v.pages,
         (select count(*)::int from kb_products c where c.version_id = v.id), v.error, v.source_id, v.version_no
  from kb_versions v where v.id = p_upload_id;
$$;

create or replace function public.kb_list(p_product_id text default null)
returns table (source_id text, product_id text, doc_kind text, source_title text, version_no int, pages int, chunks int, status text, ingested_at timestamptz)
language sql stable set search_path = public as $$
  select v.source_id, v.product_id, v.doc_kind, v.source_title, v.version_no, v.pages,
         (select count(*)::int from kb_products c where c.version_id = v.id), v.status, v.activated_at
  from kb_versions v
  where v.status in ('active','uploading') and (p_product_id is null or v.product_id = p_product_id)
  order by v.product_id, v.doc_kind;
$$;

-- Only the service role (n8n) may call these. Not the public anon/authenticated keys.
revoke all on all tables in schema public from anon, authenticated;
revoke execute on function public.kb_create_version(text,text,text,text,text), public.kb_report_stage(uuid,text,int,int),
  public.kb_verify_version(uuid), public.kb_activate_version(uuid), public.kb_fail_version(uuid,text),
  public.match_kb_products(extensions.vector,int,jsonb), public.kb_status(uuid), public.kb_list(text),
  public.kb_set_version_id() from public, anon, authenticated;
grant execute on function public.kb_create_version(text,text,text,text,text), public.kb_report_stage(uuid,text,int,int),
  public.kb_verify_version(uuid), public.kb_activate_version(uuid), public.kb_fail_version(uuid,text),
  public.match_kb_products(extensions.vector,int,jsonb), public.kb_status(uuid), public.kb_list(text) to service_role;
