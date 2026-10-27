#!/bin/sh
# Runs once, on first initialisation of the postgres_data volume.
# Creates the disposable database used by the pytest integration suite.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE DATABASE ${POSTGRES_DB}_test OWNER "$POSTGRES_USER";
SQL
