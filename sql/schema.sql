-- APIx Supabase schema.
-- Run in the Supabase SQL editor. Designed so the anon key can read published
-- index values and nothing else; raw quotes are service-role only.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------- reference
create table if not exists airports (
  iata        char(3) primary key,
  city        text not null,
  name        text,
  udf_inr     numeric(10,2),
  udf_valid_from date
);

create table if not exists basket_version (
  id            uuid primary key default gen_random_uuid(),
  version       text not null unique,
  effective_from date not null,
  weight_source text not null,
  payload       jsonb not null,          -- full basket.yaml as JSON
  created_at    timestamptz default now()
);

create table if not exists route_weights (
  basket_id  uuid references basket_version(id) on delete cascade,
  route      text not null,
  origin     char(3) not null,
  destination char(3) not null,
  weight     numeric(10,8) not null check (weight >= 0),
  primary key (basket_id, route)
);

-- ---------------------------------------------------------------- raw layer
create table if not exists fare_quotes (
  quote_id        text primary key,
  quote_ts        timestamptz not null,
  quote_date      date generated always as ((quote_ts at time zone 'UTC')::date) stored,
  origin          char(3) not null,
  destination     char(3) not null,
  route           text generated always as (origin || '-' || destination) stored,
  departure_date  date not null,
  apw_days        smallint not null,
  apw_bucket      smallint,
  carrier         varchar(3) not null,
  flight_number   text,
  cabin           text not null default 'ECONOMY',
  fare_family     text,
  stops           smallint default 0,
  duration_min    integer,
  departure_time_local text,
  total_fare      numeric(12,2),
  base_fare       numeric(12,2),
  taxes_and_fees  numeric(12,2),
  udf             numeric(10,2),
  convenience_fee numeric(10,2),
  currency        char(3) default 'INR',
  seats_available smallint,
  sold_out        boolean default false,
  source          text not null,
  collection_method text not null,
  drop_reason     text,                  -- null = used in the index
  ingested_at     timestamptz default now()
);

create index if not exists idx_quotes_cell
  on fare_quotes (quote_date, route, apw_bucket, cabin)
  where drop_reason is null;
create index if not exists idx_quotes_dep on fare_quotes (departure_date);
create index if not exists idx_quotes_carrier on fare_quotes (carrier, quote_date);

-- ---------------------------------------------------------------- index layer
create table if not exists elementary_cells (
  quote_date  date not null,
  route       text not null,
  apw         smallint not null,
  cabin       text not null default 'ECONOMY',
  price       numeric(12,2) not null,     -- Jevons geometric mean
  n_quotes    integer not null,
  n_carriers  smallint not null,
  imputed     boolean default false,
  primary key (quote_date, route, apw, cabin)
);

create table if not exists apix_daily (
  quote_date   date primary key,
  apix         numeric(10,4) not null,
  dod_pct      numeric(8,4),
  mom_pct      numeric(8,4),
  yoy_pct      numeric(8,4),
  pct_imputed  numeric(6,2),
  n_cells      integer,
  basket_id    uuid references basket_version(id),
  revision     smallint default 1,
  published_at timestamptz default now()
);

create table if not exists apix_route_daily (
  quote_date date not null,
  route      text not null,
  index_value numeric(10,4) not null,
  pct_imputed numeric(6,2),
  primary key (quote_date, route)
);

create table if not exists apix_monthly (
  month       date primary key,
  apix        numeric(10,4) not null,
  change_pct  numeric(8,4),
  dgca_avg_fare numeric(12,2),            -- for the back-test comparison
  published_at timestamptz default now()
);

-- ---------------------------------------------------------------- ops
create table if not exists collection_runs (
  id            uuid primary key default gen_random_uuid(),
  started_at    timestamptz not null default now(),
  finished_at   timestamptz,
  status        text not null default 'running',
  source        text not null,
  requests_made integer default 0,
  quotes_stored integer default 0,
  errors        jsonb default '[]'::jsonb
);

create table if not exists chat_sessions (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid references auth.users(id) on delete cascade,
  title      text,
  created_at timestamptz default now()
);

create table if not exists chat_messages (
  id         uuid primary key default gen_random_uuid(),
  session_id uuid references chat_sessions(id) on delete cascade,
  role       text not null check (role in ('user','assistant','system')),
  content    text not null,
  created_at timestamptz default now()
);

-- ---------------------------------------------------------------- RLS
alter table apix_daily         enable row level security;
alter table apix_monthly       enable row level security;
alter table apix_route_daily   enable row level security;
alter table elementary_cells   enable row level security;
alter table fare_quotes        enable row level security;
alter table chat_sessions      enable row level security;
alter table chat_messages      enable row level security;

-- Published index numbers are a public good: anyone may read them.
create policy "public read daily"   on apix_daily       for select using (true);
create policy "public read monthly" on apix_monthly     for select using (true);
create policy "public read routes"  on apix_route_daily for select using (true);
create policy "public read cells"   on elementary_cells for select using (true);

-- Raw quotes: no anon policy at all, so only the service role reaches them.
-- This matters legally as much as technically — you are not redistributing a
-- source's fare data, only the derived statistic.

create policy "own sessions" on chat_sessions
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own messages" on chat_messages
  for all using (
    exists (select 1 from chat_sessions s
            where s.id = session_id and s.user_id = auth.uid())
  );

-- ---------------------------------------------------------------- API view
create or replace view v_apix_public as
select d.quote_date, d.apix, d.dod_pct, d.mom_pct, d.yoy_pct,
       d.pct_imputed, b.version as basket_version
from apix_daily d
left join basket_version b on b.id = d.basket_id
order by d.quote_date;
