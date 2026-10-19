-- Measured reference profiles (ICC profiles or calibration-report values) per monitor model and picture mode,
-- and a reference kind for "values taken from the shared database", which never counts as a new measurement.
-- Same privacy rules as the other tables: month-only dates, network hash nulled after 60 days, deletion by code,
-- row level security with no policies.

alter table public.submissions drop constraint submissions_reference_kind_check;
alter table public.submissions add constraint submissions_reference_kind_check
  check (reference_kind in ('srgb', 'p3', 'adobe', 'icc', 'manual', 'device', 'database'));

create table public.reference_samples (
  id bigint generated always as identity primary key,
  edid_manufacturer text not null check (edid_manufacturer ~ '^[A-Z]{3}$'),
  edid_product_code integer not null check (edid_product_code between 0 and 65535),
  picture_mode text not null check (picture_mode in ('srgb', 'standard', 'user', 'native', 'cinema', 'game', 'other')),
  primaries double precision[] not null check (cardinality(primaries) = 6),   -- Rx Ry Gx Gy Bx By (CIE xy)
  white double precision[] not null check (cardinality(white) = 2),
  session_month date not null check (extract(day from session_month) = 1),
  contributor_hash bytea,           -- nulled after 60 days
  deletion_hash bytea not null,
  received_on date not null default current_date
);
create index reference_samples_key_idx on public.reference_samples (edid_manufacturer, edid_product_code, picture_mode);
create index reference_samples_deletion_idx on public.reference_samples (deletion_hash);

alter table public.reference_samples enable row level security;
-- No policies on purpose: anon and authenticated see and change nothing.
revoke all on public.reference_samples from anon, authenticated;
grant select, insert, update, delete on public.reference_samples to service_role;
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
  update public.reference_samples set contributor_hash = null
    where contributor_hash is not null and received_on < current_date - 60;
  delete from public.salts where kind = 'day' and period < to_char(current_date - 2, 'YYYY-MM-DD');
  delete from public.salts where kind = 'month' and period < to_char(current_date - 90, 'YYYY-MM');
  delete from public.submissions where status = 'pending' and received_on < current_date - 30;
$$;
