-- MindWeave (心络) Supabase / Managed PostgreSQL Role Bootstrap
-- Paste and run once in Supabase SQL Editor BEFORE deploying Cloud Run with MINDWEAVE_MIGRATE_ON_BOOT=true.
-- Replace the three CHANGE_ME_* passwords below with strong secrets and set the three MINDWEAVE_*_DATABASE_URL env vars accordingly.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mindweave_owner') THEN
    CREATE ROLE mindweave_owner LOGIN PASSWORD 'CHANGE_ME_OWNER_PASSWORD';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mindweave_app') THEN
    CREATE ROLE mindweave_app LOGIN PASSWORD 'CHANGE_ME_APP_PASSWORD' NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mindweave_system') THEN
    CREATE ROLE mindweave_system LOGIN PASSWORD 'CHANGE_ME_SYSTEM_PASSWORD' NOSUPERUSER BYPASSRLS;
  END IF;
END
$$;

GRANT USAGE, CREATE ON SCHEMA public TO mindweave_owner;
GRANT USAGE ON SCHEMA public TO mindweave_app, mindweave_system;
