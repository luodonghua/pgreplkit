# Playbook 1 — Pre-provisioned green (existing clusters, `copy` initial sync)

**When to use:** you already have both the blue (source) and green (target) PostgreSQL
clusters provisioned, and you want pgreplkit to set up logical replication and copy the
data using PostgreSQL's built-in initial copy. Works on self-managed PostgreSQL, RDS,
and Aurora.

This is also the **recommended path for a major-version upgrade** (e.g. PostgreSQL 16 →
17): provision green at the new version, then logically replicate from the old version
into it — logical replication streams across major versions.

---

## Prerequisites

- Blue (source) and green (target) reachable from where you run pgreplkit.
- Logical WAL enabled on the source:
  - self-managed: `wal_level = logical` (restart required);
  - RDS/Aurora: `rds.logical_replication = 1` in the parameter group (static — reboot).
- The **green schema already exists** (logical replication does **not** copy DDL):
  ```bash
  pg_dump --schema-only -h BLUE -d appdb | psql -h GREEN -d appdb
  ```
- The green subscriber can reach the blue publisher on 5432 (SG / firewall / pg_hba).
- Replication privileges (RDS/Aurora: `rds_replication`/`rds_superuser`; self-managed
  PG < 16: superuser).

## Configuration

`config.yml`:
```yaml
source:
  host: blue.example.com
  port: 5432
  user: postgres
  password: ${PGPASSWORD}      # or use .pgpass / secret_ref
  dbname: postgres
  # If the subscriber reaches blue on a different name/IP than you do:
  # advertised_host: blue.internal
target:
  host: green.example.com
  port: 5432
  user: postgres
  password: ${PGPASSWORD}
  dbname: postgres
project: blue-green-appdb
scope:
  databases: [appdb]           # omit to auto-discover all non-system DBs
slots:
  strategy: balanced           # single | per-schema | balanced | manual
  n: 4
lag_threshold_bytes: 0
```

## Steps

Transcribed from an actual run against a two-database source (`shop`, `inventory`):

```console
$ pgreplkit -c config.yml discover
                  pgreplkit discover — cluster topology
 database    scope      tables   schemas   note
 inventory   in-scope        2   public
 postgres    skipped         -   -         no in-scope user tables
 shop        in-scope        2   public
 template0   skipped         -   -         system database (template0)
 template1   skipped         -   -         system database (template1)
3 database(s) in scope, 3 skipped, 5 in-scope table(s).

$ pgreplkit -c config.yml preflight        # exit 10 on blocking issues
0 block, 1 warn.                            # (warn: unbounded max_slot_wal_keep_size)

$ pgreplkit -c config.yml setup --init-sync copy --slots balanced --n 2
setup complete: 2 slot(s) across 2 database(s).

$ pgreplkit -c config.yml status
 database    slot      sub       init-sync  slot     wal_status   lag(bytes)
 inventory   pgrk_…    enabled   2/2        active   reserved     0
 shop        pgrk_…    enabled   2/2        active   reserved     0
# target row counts now match source: shop 200/1000, inventory 300/300

$ pgreplkit -c config.yml validate --depth sampled
 OK    validate   -   source and target match

$ pgreplkit -c config.yml ready
READY — all slots synced, active, within lag threshold

# stop writes on blue, then:
$ pgreplkit -c config.yml cutover --writes-stopped
  ✓ quiesce: confirmed writes stopped on source
  ✓ drain: all slots synced at zero lag
  ✓ sequences: synced 0 sequence(s)
  ✓ validate: source and target match
  ✓ READY FOR CUTOVER: switch application traffic to the target now

$ pgreplkit -c config.yml teardown --yes
teardown complete.
```

Live changes stream continuously — inserting rows on the source shows `status` lag
rise then return to 0 as CDC applies them on the target (per slot).

Prefer to run everything by hand? Generate a runbook and execute nothing:
```bash
pgreplkit -c config.yml guide --init-sync copy > runbook.md
```

---

## Scenario: PostgreSQL 16 → 17 major-version upgrade (near-zero downtime)

Logical replication streams from a PG16 publisher to a PG17 subscriber, so a major
upgrade becomes: **stand up green on 17, replicate 16 → 17, cut over.**

1. **Provision green on PostgreSQL 17** (empty), same schema owners/extensions as blue.
2. **Create the schema on green** from blue:
   ```bash
   pg_dump --schema-only -h BLUE16 -d appdb | psql -h GREEN17 -d appdb
   ```
   (Review for any 17-incompatible DDL first.)
3. **Enable logical WAL on blue** (`wal_level=logical` / `rds.logical_replication=1`).
4. **Preflight** — pgreplkit reports that blue is 16 and green is 17 and gates any
   feature that isn't available on both versions:
   ```bash
   pgreplkit -c config.yml preflight
   ```
5. **Recreate globals** (roles) on green: `pgreplkit -c config.yml globals`.
6. **Setup with copy** — the initial copy loads existing data, then streaming keeps
   green current:
   ```bash
   pgreplkit -c config.yml setup --init-sync copy
   ```
7. **Watch + validate** until green is caught up and row/object counts match.
8. **Cutover** (`--writes-stopped`) — sequences are synced after writes stop, then
   validation gates the switch.
9. **Optional rollback insurance:** before opening writes on green, you can set up
   reverse replication so blue stays current:
   ```bash
   pgreplkit -c config.yml reverse       # green(17) -> blue(16), from the in-sync point
   ```
   (Reverse from 17 → 16 works for the data types in use; test in non-prod first.)

**Notes**
- The `copy` strategy is used (not snapshot-restore) because a physical snapshot keeps
  the source's major version — you cannot physically seed a 17 target from a 16
  snapshot. Logical copy is version-independent.
- Very large datasets: `copy` can be slow. If downtime budget allows a physical seed at
  the *same* version first, see the RDS-to-RDS / Aurora-to-Aurora playbooks, then
  major-upgrade green in place before wiring — but the simplest correct path is `copy`.
