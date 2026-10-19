-- Monitor settings people chose to share: the Red, Green and Blue values from the monitor's menu,
-- whether the person calibrated it or left it as it was, and how the server measured the result.
-- Same privacy rules as submissions: month-only dates, a network hash nulled after 60 days, deletion
-- by the sender's code. Row level security denies everything to anon and authenticated.

create table public.settings_samples (
  id bigint generated always as identity primary key,
  edid_manufacturer text not null check (edid_manufacturer ~ '^[A-Z]{3}$'),
  edid_product_code integer not null check (edid_product_code between 0 and 65535),
  picture_mode text not null check (picture_mode in ('srgb', 'standard', 'user', 'native', 'cinema', 'game', 'other')),
  calibrated boolean not null,
  rgb_r real not null check (rgb_r > 0 and rgb_r <= slider_max),
  rgb_g real not null check (rgb_g > 0 and rgb_g <= slider_max),
  rgb_b real not null check (rgb_b > 0 and rgb_b <= slider_max),
  slider_max real not null check (slider_max between 1 and 1000),
  white_cct real not null,          -- recomputed by the server from the readings, never taken from the client
  white_de real not null,
  white_duv real not null,
  gamma real,
  max_tint real,
  phone_id bigint references public.phones on delete set null,
  session_month date not null check (extract(day from session_month) = 1),
  contributor_hash bytea,           -- nulled after 60 days
  deletion_hash bytea not null,
  received_on date not null default current_date
);
create index settings_samples_key_idx on public.settings_samples (edid_manufacturer, edid_product_code, picture_mode);
create index settings_samples_deletion_idx on public.settings_samples (deletion_hash);

alter table public.settings_samples enable row level security;
-- No policies on purpose: anon and authenticated see and change nothing.
revoke all on public.settings_samples from anon, authenticated;
grant select, insert, update, delete on public.settings_samples to service_role;
grant usage, select on all sequences in schema public to service_role;

create or replace function public.purge_expired() returns void
language sql
set search_path = ''
as $$
  delete from public.rate_limits where window_start < now() - interval '48 hours';
  delete from public.used_nonces where expires < now();
  update public.submissions set contributor_hash = null
    where contributor_hash is not null and received_on < current_date - 60;
  update public.settings_samples set contributor_hash = null
    where contributor_hash is not null and received_on < current_date - 60;
  delete from public.salts where kind = 'day' and period < to_char(current_date - 2, 'YYYY-MM-DD');
  delete from public.salts where kind = 'month' and period < to_char(current_date - 90, 'YYYY-MM');
  delete from public.submissions where status = 'pending' and received_on < current_date - 30;
$$;
