#!/usr/bin/env bash
# restart Postgres, then commit a version before the hub can reconnect
set -e
runuser -u nobody -- /data/flagflow/.devenv/pg-linux/bin/pg_ctl -D /data/flagflow/.devenv/pgdata-linux -l /data/flagflow/.devenv/pgdata-linux/pg.log restart -m fast -w > /dev/null
v=$(PGPASSWORD=postgres /data/flagflow/.devenv/pg-linux/bin/psql -h 127.0.0.1 -p 54329 -U postgres -d featureflags-dev -Atq -c "BEGIN; WITH p AS (UPDATE project SET version = version + 1 WHERE name = 'loadtest' RETURNING name, version) SELECT version, pg_notify('featureflags_project_version', json_build_object('project', name, 'version', version)::text) FROM p; COMMIT;" | cut -d'|' -f1)
echo "$v,$(date +%s.%N)" >> /tmp/claude-0/-data/a92ac85b-873e-4072-9e66-81de36c86f1a/scratchpad/lt-fault-pg2/published.csv
echo "race version $v committed at $(date +%T.%N)"
