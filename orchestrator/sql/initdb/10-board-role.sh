#!/bin/sh
# First start of the factory's database only (Postgres runs files in /docker-entrypoint-initdb.d once,
# on an empty data folder). Creates the board's read-only role. `make db-board` does the same later.
set -eu
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v pw="$FACTORY_BOARD_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE factory_board LOGIN PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factory_board') \gexec
GRANT CONNECT ON DATABASE factory TO factory_board;
GRANT USAGE ON SCHEMA public TO factory_board;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO factory_board;
ALTER DEFAULT PRIVILEGES FOR ROLE factory IN SCHEMA public GRANT SELECT ON TABLES TO factory_board;
SQL
