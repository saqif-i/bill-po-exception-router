#!/usr/bin/env bash
# Executes ONCE, on first initialisation of the postgres data volume.
#
# The bootstrap superuser credential is consumed only here. It is never passed
# to the policy service, to demo_seed, or to n8n (Volume 01 section 9.5).
#
# The n8n boundary is stated by explicit REVOKE rather than left implicit.
#
# To be precise about what that buys: REVOKE removes a grant. It does not make
# the boundary permanent, because a future GRANT or a role membership could
# restore access. That is exactly why `make check-db` exists and why CI runs it
# on every push: the boundary is asserted continuously, not assumed from one
# statement here (ADR-002).
set -euo pipefail

APP_DB="${APP_DB_NAME:-bpr}"
N8N_DB="${N8N_DB_NAME:-n8n}"
OWNER="${BPR_OWNER_USER:-bpr_owner}"
N8N_USER="${N8N_DB_USER:-n8n_app}"

: "${BPR_OWNER_PASSWORD:?BPR_OWNER_PASSWORD is required}"
: "${N8N_DB_PASSWORD:?N8N_DB_PASSWORD is required}"

# Values are passed as psql variables and quoted by psql itself, rather than
# interpolated into the SQL text. A generated password containing an apostrophe
# would otherwise break initialisation, and the failure would look like a
# corrupt image rather than a quoting bug.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
     -v owner="$OWNER" -v owner_pw="$BPR_OWNER_PASSWORD" \
     -v n8n_user="$N8N_USER" -v n8n_pw="$N8N_DB_PASSWORD" \
     -v app_db="$APP_DB" -v n8n_db="$N8N_DB" <<-SQL
    -- 3. owner role. CREATEROLE so migration 001 can create the runtime roles
    --    without ever handing out the superuser credential.
    CREATE ROLE :"owner" LOGIN CREATEROLE PASSWORD :'owner_pw';

    -- 4. n8n login role.
    CREATE ROLE :"n8n_user" LOGIN PASSWORD :'n8n_pw';

    -- 1, 2. the two databases.
    CREATE DATABASE :"app_db" OWNER :"owner";
    CREATE DATABASE :"n8n_db" OWNER :"n8n_user";

    -- 5. n8n may reach its own database.
    GRANT CONNECT ON DATABASE :"n8n_db" TO :"n8n_user";

    -- 6, 7. the boundary, stated explicitly rather than implied.
    REVOKE CONNECT ON DATABASE :"app_db" FROM :"n8n_user";
    REVOKE CONNECT ON DATABASE :"app_db" FROM PUBLIC;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$N8N_DB" \
     -v n8n_user="$N8N_USER" <<-SQL
    GRANT ALL ON SCHEMA public TO :"n8n_user";
SQL

echo "bootstrap: databases ${APP_DB} and ${N8N_DB} created; ${N8N_USER} cannot connect to ${APP_DB}"