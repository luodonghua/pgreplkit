# Playbook 2 — RDS-to-RDS (snapshot-restore seeded logical replication)

**When to use:** source and target are both **Amazon RDS for PostgreSQL** (same major
version), the dataset is large enough that the built-in `copy` initial sync would be
too slow or too heavy on the source, and you want a fast physical seed via an RDS
snapshot restore followed by logical CDC.

pgreplkit can either **provision** the green instance for you (restore the snapshot via
boto3) or wire replication to a green you restored yourself.

Why this works: an RDS→RDS snapshot restore **preserves LSN continuity**, so
`rds_tools.logical_seed_lsn()` on the restored target returns the exact snapshot point,
and replication resumes from there **exactly once**.

---

## The load-bearing ordering (why pgreplkit does it this way)

1. On the source: **`CREATE PUBLICATION`, then create the logical slot** — in that
   order. Logical decoding replays from the slot's `restart_lsn`, so the publication
   must already exist at that point (otherwise the apply worker fails with
   *"publication does not exist"*).
2. Take the RDS snapshot (contains the publication + data).
3. Restore the green instance from the snapshot.
4. On green: `SELECT rds_tools.logical_seed_lsn();` → the seed LSN.
5. On green: `CREATE SUBSCRIPTION ... (copy_data=false, create_slot=false,
   enabled=false, slot_name=...)`.
6. `pg_replication_origin_advance('pg_<suboid>', <seed_lsn>)` → **exactly-once boundary**.
7. `ALTER SUBSCRIPTION ... ENABLE`.

`pgreplkit setup --mode provision --init-sync snapshot-restore` performs all of this.

## Prerequisites

- Source RDS with `rds.logical_replication = 1` (parameter group, static → reboot).
- IAM permissions for snapshot + restore (`rds:CreateDBSnapshot`,
  `rds:RestoreDBInstanceFromDBSnapshot`, `rds:CreateDBInstance`, parameter/subnet-group
  and security-group access).
- **Networking:** the restored green must reach the source publisher on 5432. RDS
  public endpoints can resolve to the *public* IP even within a VPC, so either:
  - set `advertised_host` to the source's **private IP** and allow the VPC CIDR on the
    security group (recommended), or
  - allow both instances' public IPs on the security group.

## Configuration

`config.yml`:
```yaml
source:
  host: blue.abc123.us-east-1.rds.amazonaws.com
  port: 5432
  user: postgres
  password: ${PGPASSWORD}
  dbname: postgres
  advertised_host: 172.31.10.20      # source PRIVATE IP (green reaches it intra-VPC)
provision_mode: provision            # let pgreplkit restore the green instance
init_sync: snapshot-restore
project: rds-migration
scope:
  databases: [appdb]
slots:
  strategy: balanced
  n: 4
aws:
  profile: workshop
  region: us-east-1
  source_instance_id: blue
  target_instance_id: green
  target_instance_class: db.r6g.large
  security_group_ids: [sg-0123456789]
  parameter_group: pg16-logical
  publicly_accessible: true
```

## Steps

```bash
# 1. Preflight the source (read-only)
pgreplkit -c config.yml preflight

# 2. Provision + seed + wire in one wired command:
#    prepare source (pub+slot) -> snapshot -> restore green -> capture seed LSN ->
#    wire seed-resume (subscription -> origin advance -> enable)
pgreplkit -c config.yml setup --mode provision --init-sync snapshot-restore

# 3. Monitor lag/slot health until caught up
pgreplkit -c config.yml watch

# 4. Validate + cutover
pgreplkit -c config.yml validate --depth sampled
pgreplkit -c config.yml cutover --writes-stopped

# 5. Tear down replication artifacts when done
pgreplkit -c config.yml teardown --yes
```

**Bring-your-own green** (you restored the snapshot yourself): set `provision_mode:
existing` and provide the `target:` endpoint; pgreplkit skips the snapshot/restore and
performs steps 4–7 (capture seed LSN + wire) against your restored green. In this mode
you must create the publication + slot on the source **before** you take the snapshot.

Generate-only runbook (no execution):
```bash
pgreplkit -c config.yml guide --init-sync snapshot-restore > runbook.md
```

---

## Scenario: PostgreSQL 16 → 17 major-version upgrade

A physical snapshot restore keeps the **same** major version, so you cannot seed a 17
target directly from a 16 snapshot. Two supported patterns:

### Pattern A — restore, then upgrade green, then logical CDC (fast seed + upgrade)

The physical restore and the major upgrade are **both done to green**; the resume point
is a **source-timeline** LSN captured *before* the upgrade, so the upgrade never
invalidates it (the subscription's replication origin tracks the **source** LSN, not
green's local LSN). Ordered steps:

1. **On the 16 source**, create the publication then the logical slot (`pgreplkit`'s
   `prepare_source` does pub-then-slot). The slot pins WAL retention on the source from
   its `restart_lsn`, so nothing committed after this point can be lost.
2. **Restore green from the 16 snapshot** (same version, 16 — fast physical seed) and,
   **while green is still 16**, capture the seed LSN on green with
   `SELECT rds_tools.logical_seed_lsn();`. **Save this value.** It is a point on the
   *source's* WAL timeline and is ≥ the slot's `restart_lsn`.
3. **Major-version-upgrade green to 17** in place (`modify-db-instance --engine-version
   17.x --allow-major-version-upgrade`, or the console). The upgrade rewrites green's
   *local* LSN lineage, but that does **not** matter — you already captured the
   source-side seed LSN, and it is what the resume uses.
4. **On the upgraded 17 green**, create the subscription **reusing the source slot** and
   advance the origin to the pre-upgrade seed LSN, then enable:
   ```sql
   CREATE SUBSCRIPTION sub CONNECTION '...' PUBLICATION pub
     WITH (copy_data = false, create_slot = false, enabled = false, slot_name = '<slot>');
   SELECT 'pg_'||oid FROM pg_subscription WHERE subname = 'sub';   -- -> pg_<oid>
   SELECT pg_replication_origin_advance('pg_<oid>', '<seed_lsn>'); -- the pre-upgrade LSN
   ALTER SUBSCRIPTION sub ENABLE;
   ```
   This is **exactly-once**: data at or before the seed LSN is already in the physical
   restore (no duplicates), and the source slot retained the WAL after it (no loss).
5. `validate` → `cutover` (optional `reverse` for rollback).

Because `pgreplkit`'s physical-seed flow captures the seed LSN and wires this resume in
a single run, the upgrade variant is best driven from the **generate-only runbook**
(`guide --init-sync snapshot-restore`): run it by hand and slot the green major upgrade
in **between step 2 (seed-LSN capture) and step 4 (CREATE SUBSCRIPTION)**. This is the
same sequence verified end-to-end in **Playbook 5** (Aurora 17 → 18).

### Pattern B — pure logical (simplest, version-independent)
Provision green directly on PostgreSQL 17 and use **Playbook 1** with
`--init-sync copy`. No snapshot involved; logical replication streams 16 → 17. Prefer
this unless the dataset is too large for a logical copy.

**Recommendation:** use Pattern B unless physical seed speed is essential. If it is, use
Pattern A and capture the seed LSN **before** the green upgrade, then reuse the source
slot and `pg_replication_origin_advance` to it — the post-upgrade state of green does
not affect the resume point.
