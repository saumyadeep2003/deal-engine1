-- SQLite-dialect compatibility layer for Postgres (Supabase).
--
-- The engine was written against SQLite. Rather than rewriting every query
-- (and re-introducing bugs in code that is already tested), Postgres is taught
-- the handful of SQLite functions the codebase uses. Queries stay identical
-- across both backends; the only translation the driver does is ? -> %s.

-- julianday(text) — SQLite's julian day number, fractional.
CREATE OR REPLACE FUNCTION julianday(t text) RETURNS double precision AS $$
  SELECT CASE
    WHEN t IS NULL THEN NULL
    WHEN lower(t) = 'now' THEN extract(epoch from now()) / 86400.0 + 2440587.5
    ELSE extract(epoch from (replace(t, 'T', ' ')::timestamptz)) / 86400.0 + 2440587.5
  END;
$$ LANGUAGE sql STABLE;

-- sqlite modifier string ('-30 days', '+72 hours', 'localtime') -> interval
CREATE OR REPLACE FUNCTION _sqlite_modifier(m text) RETURNS interval AS $$
  SELECT CASE
    WHEN m IS NULL OR m = '' OR lower(m) IN ('localtime', 'utc') THEN interval '0'
    ELSE replace(m, '+', '')::interval
  END;
$$ LANGUAGE sql IMMUTABLE;

-- datetime('now'[, modifier]) / datetime(col[, modifier]) -> 'YYYY-MM-DD HH24:MI:SS'
CREATE OR REPLACE FUNCTION datetime(t text, m text DEFAULT NULL) RETURNS text AS $$
  SELECT to_char(
    (CASE WHEN lower(t) = 'now' THEN now()
          ELSE replace(t, 'T', ' ')::timestamptz END) + _sqlite_modifier(m),
    'YYYY-MM-DD HH24:MI:SS');
$$ LANGUAGE sql STABLE;

-- NOTE: no date() compat function — Postgres resolves date('x') as a cast to
-- the date TYPE, which shadows any function and silently changes semantics.
-- The three former date() call sites compute their day-strings in Python.

-- GROUP_CONCAT(x) / GROUP_CONCAT(DISTINCT x) — SQLite's comma-joined aggregate.
CREATE OR REPLACE FUNCTION _gc_step(acc text, x text) RETURNS text AS $$
  SELECT CASE WHEN x IS NULL THEN acc
              WHEN acc IS NULL OR acc = '' THEN x
              ELSE acc || ',' || x END;
$$ LANGUAGE sql IMMUTABLE;
DO $$ BEGIN
  CREATE AGGREGATE group_concat(text) (SFUNC = _gc_step, STYPE = text);
EXCEPTION WHEN duplicate_function THEN NULL; END $$;
