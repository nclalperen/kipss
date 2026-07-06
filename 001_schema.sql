-- KPSS Quiz App — Schema Migration 001
-- Paste into Supabase SQL Editor and run once.

-- Two hardcoded users, no auth.
create table users (
  user_id   text primary key,          -- 'alperen', 'hatice'
  display_name text not null
);

insert into users (user_id, display_name) values
  ('alperen', 'Alperen'),
  ('hatice',  'Hatice');

create table questions (
  q_id           uuid primary key default gen_random_uuid(),
  category       text not null check (category in ('tarih','cografya','vatandaslik','guncel')),
  source         text not null,        -- e.g. 'KPSS-2024-GK'
  question_no    int,                  -- original number in the PDF, for verification
  question_text  text not null,
  options        jsonb not null,       -- {"A": "...", "B": "...", "C": "...", "D": "...", "E": "..."}
  correct_answer char(1) not null check (correct_answer in ('A','B','C','D','E')),
  has_image      boolean not null default false,  -- skip these in v1 serving
  created_at     timestamptz not null default now()
);

create table attempts (
  attempt_id    uuid primary key default gen_random_uuid(),
  user_id       text not null references users(user_id),
  q_id          uuid not null references questions(q_id),
  chosen_answer char(1),               -- null = timed out
  is_correct    boolean not null,
  time_taken_ms int,
  session_id    uuid,                  -- groups one sitting; used later for duo compare
  created_at    timestamptz not null default now()
);

create index idx_attempts_user_q on attempts (user_id, q_id);
create index idx_questions_category on questions (category);

-- Priority queue: unseen first, then error-weighted with correct-decay.
create or replace view v_question_priority as
select
  q.q_id,
  q.category,
  u.user_id,
  coalesce(sum(case when a.is_correct = false then 1 else 0 end), 0) as wrong_count,
  coalesce(sum(case when a.is_correct = true  then 1 else 0 end), 0) as correct_count,
  case
    when count(a.attempt_id) = 0 then 3.0                              -- unseen boost
    else greatest(0.2,
      1.0
      + 2.0 * sum(case when a.is_correct = false then 1 else 0 end)    -- error multiplier
      - 0.7 * sum(case when a.is_correct = true  then 1 else 0 end))   -- correct decay
  end as weight
from questions q
cross join users u
left join attempts a on a.q_id = q.q_id and a.user_id = u.user_id
where q.has_image = false
group by q.q_id, q.category, u.user_id;

-- Frontend query per user/category:
--   select * from v_question_priority
--   where user_id = :uid and category = :cat
--   order by weight desc, random() limit 20;

-- v1 simplicity: RLS stays OFF (anon key read/write between two trusted users).
-- Revisit only if the URL ever leaves your two devices.
