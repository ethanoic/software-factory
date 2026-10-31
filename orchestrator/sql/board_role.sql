-- Read-only database role for the factory board. Run once by `make db-board` as the Postgres superuser,
-- with the password passed as the psql variable :pw. Safe to run again.
SELECT format('CREATE ROLE factory_board LOGIN PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factory_board') \gexec
SELECT format('ALTER ROLE factory_board LOGIN PASSWORD %L', :'pw') \gexec
GRANT CONNECT ON DATABASE factory TO factory_board;
\connect factory
GRANT USAGE ON SCHEMA public TO factory_board;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO factory_board;
-- Tables the factory creates later (new migrations) become readable too.
ALTER DEFAULT PRIVILEGES FOR ROLE factory IN SCHEMA public GRANT SELECT ON TABLES TO factory_board;
