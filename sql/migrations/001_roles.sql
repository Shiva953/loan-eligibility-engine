-- Application login roles (docs/DECISIONS.md AD1).
--
-- No passwords live here: scripts/migrate.py sets them from .env after the migrations run,
-- so this file is identical for local Postgres and RDS. Roles are cluster-wide, which is why
-- every statement is guarded and safe to re-run against another database on the same server.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'n8n_app') THEN
        CREATE ROLE n8n_app LOGIN;
    END IF;

    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'lambda_app') THEN
        CREATE ROLE lambda_app LOGIN;
    END IF;

    -- rds_iam exists only on Amazon RDS. Membership switches lambda_app to IAM token
    -- authentication, so the in-VPC Lambdas never need a stored password.
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'rds_iam') THEN
        GRANT rds_iam TO lambda_app;
    END IF;
END
$$;
