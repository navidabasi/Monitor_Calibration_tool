-- Shared phone corrections: tables, row level security, helper functions and scheduled cleanup.
--
-- Privacy properties (docs/PRIVACY.md): no IP addresses, no exact timestamps (a
-- session has a month, a submission a day), the network hash on a submission is
-- nulled after 60 days, rate-limit rows live 48 hours, salts are deleted when
-- their period is over, and a submission can be deleted with a code only its
-- sender knows (only the code's SHA-256 is stored).
--
-- Access: only the service role (the API) touches these tables. Every table has
-- row level security enabled and NO policies for anon or authenticated, which
-- denies them everything. The explicit revokes are a second lock.

create extension if not exists pgcrypto with schema extensions;

-- ---------------------------------------------------------------- tables

create table public.phones (
  id bigint generated always as identity primary key,
  unique_camera_model text not null check (length(unique_camera_model) between 1 and 80),
  lens_model text not null default '' check (length(lens_model) <= 80),
  raw_type text not null check (raw_type in ('bayer', 'linear')),
  app_family text not null check (app_family in ('lightroom', 'apple', 'open_camera', 'samsung', 'google', 'other')),
  embedded_color_matrix double precision[] check (embedded_color_matrix is null or cardinality(embedded_color_matrix) = 9),
  status text not null default 'pending' check (status in ('pending', 'verified', 'blocked')),
  unique (unique_camera_model, lens_model, raw_type, app_family)
);

create table public.monitors (
  id bigint generated always as identity primary key,
  edid_manufacturer text not null default '' check (edid_manufacturer ~ '^([A-Z]{3})?$'),
  edid_product_code integer not null default -1 check (edid_product_code between -1 and 65535),
  model_name text not null default '' check (length(model_name) <= 64),
  screen_tech text not null default 'unknown'
    check (screen_tech in ('woled', 'qdoled', 'oled_rgb', 'ips_ksf', 'ips_wled', 'va_qd', 'va_wled', 'mini_led_qd', 'unknown')),
  status text not null default 'pending' check (status in ('pending', 'verified', 'blocked')),
  unique (edid_manufacturer, edid_product_code, model_name)
);

create table public.submissions (
  id bigint generated always as identity primary key,
  session_month date not null check (extract(day from session_month) = 1),   -- first day of the month only
  app_version text not null check (length(app_version) <= 20),
  analysis_version integer not null,
  phone_id bigint not null references public.phones on delete cascade,
  reference_monitor_id bigint references public.monitors on delete set null,
  target_monitor_id bigint references public.monitors on delete set null,
  reference_kind text not null check (reference_kind in ('srgb', 'p3', 'adobe', 'icc', 'manual', 'device')),
  reference_screen_tech text not null
    check (reference_screen_tech in ('woled', 'qdoled', 'oled_rgb', 'ips_ksf', 'ips_wled', 'va_qd', 'va_wled', 'mini_led_qd', 'unknown')),
  target_picture_mode text check (target_picture_mode in ('srgb', 'standard', 'user', 'native', 'cinema', 'game', 'other')),
  capture jsonb not null,           -- allow-listed DNG fields
  measurements jsonb not null,      -- patch readings, marks, framing, quality
  server_result jsonb not null,     -- recomputed matrices and checks
  status text not null default 'pending' check (status in ('pending', 'accepted', 'rejected', 'quarantined')),
  contributor_hash bytea,           -- HMAC(network prefix, monthly salt); nulled after 60 days
  deletion_hash bytea not null,     -- SHA-256 of the sender's deletion code
  received_on date not null default current_date
);
create index submissions_status_idx on public.submissions (status, phone_id);
create index submissions_deletion_idx on public.submissions (deletion_hash);

create table public.corrections (
  id bigint generated always as identity primary key,
  phone_id bigint not null references public.phones on delete cascade,
  monitor_id bigint references public.monitors on delete cascade,   -- null: filed under the screen technology
  screen_tech text not null,
  matrix double precision[] not null check (cardinality(matrix) = 9),
  contributors integer not null,
  spread_k real,
  analysis_version integer,
  frozen boolean not null default false,   -- held for review by scripts/review.py; never served
  updated_on date
);
-- One row per phone, monitor (or none) and screen technology.
create unique index corrections_key on public.corrections (phone_id, coalesce(monitor_id, 0), screen_tech);

create table public.rate_limits (
  key_hash bytea not null,
  window_start timestamptz not null,
  count integer not null default 0,
  primary key (key_hash, window_start)
);

create table public.used_nonces (
  nonce_hash bytea primary key,
  expires timestamptz not null
);

create table public.salts (
  kind text not null check (kind in ('day', 'month')),
  period text not null,
  salt bytea not null,
  primary key (kind, period)
);

-- ---------------------------------------------------------------- row level security

alter table public.phones enable row level security;
alter table public.monitors enable row level security;
alter table public.submissions enable row level security;
alter table public.corrections enable row level security;
alter table public.rate_limits enable row level security;
alter table public.used_nonces enable row level security;
alter table public.salts enable row level security;
-- No policies on purpose: with RLS on and no policy, anon and authenticated see and change nothing.

revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from anon, authenticated, public;
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated, public;

-- ---------------------------------------------------------------- functions (called by the API with the service role)
-- SECURITY INVOKER (the default): they run with the caller's rights, which is the service role.

create function public.get_or_create_salt(p_kind text, p_period text) returns bytea
language plpgsql
set search_path = ''
as $$
declare
  s bytea;
begin
  insert into public.salts (kind, period, salt) values (p_kind, p_period, extensions.gen_random_bytes(32))
    on conflict do nothing;
  select salt into s from public.salts where kind = p_kind and period = p_period;
  return s;
end;
$$;

create function public.rate_hit(p_key bytea, p_window timestamptz, p_limit integer) returns boolean
language plpgsql
set search_path = ''
as $$
declare
  n integer;
begin
  insert into public.rate_limits (key_hash, window_start, count) values (p_key, p_window, 1)
    on conflict (key_hash, window_start) do update set count = public.rate_limits.count + 1
    returning count into n;
  return n <= p_limit;
end;
$$;

create function public.use_nonce(p_hash bytea, p_expires timestamptz) returns boolean
language plpgsql
set search_path = ''
as $$
declare
  inserted integer;
begin
  insert into public.used_nonces (nonce_hash, expires) values (p_hash, p_expires) on conflict do nothing;
  get diagnostics inserted = row_count;
  return inserted = 1;
end;
$$;

revoke all on function public.get_or_create_salt(text, text) from public, anon, authenticated;
revoke all on function public.rate_hit(bytea, timestamptz, integer) from public, anon, authenticated;
revoke all on function public.use_nonce(bytea, timestamptz) from public, anon, authenticated;
grant execute on function public.get_or_create_salt(text, text) to service_role;
grant execute on function public.rate_hit(bytea, timestamptz, integer) to service_role;
grant execute on function public.use_nonce(bytea, timestamptz) to service_role;
grant select, insert, update, delete on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;

-- ---------------------------------------------------------------- scheduled cleanup (pg_cron)
-- Enable the extension once in the Supabase dashboard (Database -> Extensions), then these run on their own.

create function public.purge_expired() returns void
language sql
set search_path = ''
as $$
  delete from public.rate_limits where window_start < now() - interval '48 hours';
  delete from public.used_nonces where expires < now();
  update public.submissions set contributor_hash = null
    where contributor_hash is not null and received_on < current_date - 60;
  delete from public.salts where kind = 'day' and period < to_char(current_date - 2, 'YYYY-MM-DD');
  delete from public.salts where kind = 'month' and period < to_char(current_date - 90, 'YYYY-MM');
  delete from public.submissions where status = 'pending' and received_on < current_date - 30;
$$;
revoke all on function public.purge_expired() from public, anon, authenticated;
grant execute on function public.purge_expired() to service_role;

do $$
begin
  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    perform cron.schedule('purge-expired', '17 * * * *', 'select public.purge_expired()');
  else
    raise notice 'pg_cron is not enabled: enable it, then run: select cron.schedule(''purge-expired'', ''17 * * * *'', ''select public.purge_expired()'');';
  end if;
end;
$$;
