#!/usr/bin/env bash
# Makes .env.factory complete before the factory starts (run by `make init` and `make up`).
#  - Creates it from .env.factory.example if it is missing (mode 600).
#  - Adds any setting the example has that it lacks (older copies miss later additions).
#  - Generates the two database passwords if they are blank or still the example's placeholder,
#    but only before the database exists: once created, Postgres keeps its first passwords.
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=.env.factory
EXAMPLE=.env.factory.example
DB_VOLUME=${FACTORY_DB_VOLUME:-factory_factory-db}

if [ ! -f "$ENV" ]; then
  cp "$EXAMPLE" "$ENV"
  echo "created $ENV"
fi
chmod 600 "$ENV"

value() { grep -E "^$1=" "$ENV" | tail -n 1 | cut -d= -f2- || true; }

set_value() {   # replace every KEY= line, or append one; portable across macOS and Linux sed
  local key=$1 val=$2 tmp
  tmp=$(mktemp "$ENV.XXXXXX")
  grep -vE "^$key=" "$ENV" > "$tmp" || true
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  chmod 600 "$tmp" && mv "$tmp" "$ENV"
}

# 1. settings the example has and this file lacks
added=()
while IFS= read -r line; do
  key=${line%%=*}
  grep -qE "^$key=" "$ENV" || { printf '%s\n' "$line" >> "$ENV"; added+=("$key"); }
done < <(grep -E '^[A-Z][A-Z0-9_]*=' "$EXAMPLE")
[ ${#added[@]} -eq 0 ] || echo "added missing settings to $ENV: ${added[*]}"

# 2. database passwords. Docker must answer first: "no database volume" has to mean exactly that.
if ! docker info > /dev/null 2>&1; then
  echo "error: Docker is not running. Start Docker Desktop, then run this again." >&2
  exit 1
fi
db_exists=no
if docker volume inspect "$DB_VOLUME" > /dev/null 2>&1; then db_exists=yes; fi
for key in FACTORY_DB_PASSWORD FACTORY_BOARD_DB_PASSWORD; do
  v=$(value "$key")
  case "$v" in
    "" | change-me | change-me-too) ;;
    *) continue ;;
  esac
  if [ "$db_exists" = no ]; then
    set_value "$key" "$(openssl rand -hex 24)"
    echo "generated $key in $ENV"
  elif [ "$key" = FACTORY_BOARD_DB_PASSWORD ]; then
    set_value "$key" "$(openssl rand -hex 24)"
    echo "generated $key in $ENV. The database already exists, so after make up, run: make db-board"
  else
    cat >&2 <<EOF
error: $key in $ENV is ${v:-blank}, but the factory's database already exists ($DB_VOLUME)
and keeps the password it was created with. Put that password back in $ENV.
Only if this is a fresh setup with nothing worth keeping, delete the database instead:
  docker volume rm $DB_VOLUME
then run make up again, and a new password is generated.
EOF
    exit 1
  fi
done
