# Playbook 3 — Aurora-to-Aurora (fast-clone seeded logical replication)

**When to use:** source and target are both **Amazon Aurora PostgreSQL** (same major
version) and you want the fastest possible physical seed. Aurora **fast cloning** is
copy-on-write against the shared cluster volume, so the green cluster is available in
minutes regardless of data size — then logical replication streams the delta.

> This playbook is transcribed from an **actual verified run** on real Aurora
> PostgreSQL **16.4.7** (`db.t4g.medium`) in us-east-1. The exactly-once result and
> function outputs shown are real.

Why this works: an Aurora fast clone is copy-on-write and preserves LSN continuity, so
`aurora_volume_logical_start_lsn()` on the clone returns the consistent start LSN, and
replication resumes from there **exactly once**.

**Verified fact (worth knowing):** `aurora_volume_logical_start_lsn()` **is available on
Aurora PostgreSQL 16** and returns a valid LSN (e.g. `0/44759B8`), even though the AWS
documentation page lists only versions ≤ 15. The RDS function
`rds_tools.logical_seed_lsn()` does **not** exist on Aurora — it is RDS-only.

---

## The ordering (same load-bearing sequence as RDS-to-RDS)

1. On the source: **`CREATE PUBLICATION`, then create the logical slot** (pub first —
   logical decoding replays from the slot's `restart_lsn`, so the publication must
   exist at that point).
2. Create the Aurora **fast clone** (`restore-db-cluster-to-point-in-time
   --restore-type copy-on-write --use-latest-restorable-time`) + a writer instance.
3. On the clone: `SELECT aurora_volume_logical_start_lsn();` → the seed LSN.
4. On the clone: `CREATE SUBSCRIPTION ... (copy_data=false, create_slot=false,
   enabled=false, slot_name=...)`.
5. `pg_replication_origin_advance('pg_<suboid>', <seed_lsn>)` → exactly-once boundary.
6. `ALTER SUBSCRIPTION ... ENABLE`.

`pgreplkit setup --mode provision --init-sync aurora-fast-clone` performs all of this
(engine auto-detected via `aurora_version()`).

## Prerequisites

- Source Aurora cluster with `rds.logical_replication = 1` (cluster parameter group;
  static → reboot).
- IAM permissions: `rds:RestoreDBClusterToPointInTime`, `rds:CreateDBInstance`,
  and networking (subnet/security-group access).
- Networking: the clone's writer must reach the source writer on 5432 — set the source
  writer's **private IP** as `advertised_host` and allow the VPC CIDR on the security
  group (Aurora public endpoints hairpin to public IPs within a VPC, and the OS may
  cache a reused endpoint's old IP).
- Same major version on source and clone (a clone is always the same version).

## Configuration

`config.yml`:
```yaml
source:
  host: pgrepl-aur-src.cluster-xxxx.us-east-1.rds.amazonaws.com   # cluster writer endpoint
  port: 5432
  user: postgres
  password: ${PGPASSWORD}
  dbname: postgres
  advertised_host: 172.31.26.173      # source writer PRIVATE IP (clone reaches it intra-VPC)
provision_mode: provision
init_sync: aurora-fast-clone
project: aurora-migration
scope:
  databases: [appdb]
slots: {strategy: balanced, n: 6}
aws:
  profile: workshop
  region: us-east-1
  source_instance_id: pgrepl-aur-src       # source cluster identifier to clone
  target_cluster_id: pgrepl-aur-clone
  target_instance_class: db.t4g.medium
  security_group_ids: [sg-0b903dd25b343f323]
  parameter_group: pgrepl-aurora-logical   # aurora cluster parameter group
  publicly_accessible: true
```

## Steps

```bash
pgreplkit -c config.yml preflight
pgreplkit -c config.yml setup --mode provision --init-sync aurora-fast-clone
pgreplkit -c config.yml watch
pgreplkit -c config.yml validate --depth sampled
pgreplkit -c config.yml cutover --writes-stopped
pgreplkit -c config.yml --yes teardown
```

**Verified exactly-once run (real output):**
```
engine detected on clone = aurora (seed sql: SELECT aurora_volume_logical_start_lsn())
seed_lsn=0/44759B8 clone_rows=50
PASS: Aurora->Aurora fast-clone seed-resume — clone 50 -> 70 (source 70), distinct=70
```
(50 rows seeded via the clone; 20 rows inserted on the source after the clone point
streamed exactly once — no duplicate-key errors, final distinct count = 70.)

**Bring-your-own clone:** set `provision_mode: existing` with a `target:` endpoint (a
clone you created via the console/CLI); pgreplkit captures
`aurora_volume_logical_start_lsn()` on it and wires the seed-resume. Create the
publication + slot on the source **before** cloning.

Generate-only runbook:
```bash
pgreplkit -c config.yml guide --init-sync aurora-fast-clone > runbook.md
```

---

## Scenario: PostgreSQL 16 → 17 major-version upgrade

An Aurora clone is always the **same** major version as the source, so you cannot clone
a 17 green directly from a 16 source.

### Pattern A — clone, upgrade the clone to 17, then logical CDC

The resume point is a **source-timeline** seed LSN captured on the clone *before* the
upgrade, so upgrading the clone never invalidates it — the subscription's replication
origin tracks the **source** LSN, not the clone's local LSN. This is the sequence
verified end-to-end in **Playbook 5** (Aurora 17 → 18):

1. **On the 16 source**, create the publication then the logical slot (pub-then-slot);
   the slot pins WAL retention from its `restart_lsn`.
2. **Fast-clone green from the 16 source** (minutes, copy-on-write) and, **while the
   clone is still 16**, capture the seed LSN on it with
   `SELECT aurora_volume_logical_start_lsn();` and **save it**. It is a point on the
   source's WAL timeline (≥ the slot's `restart_lsn`).
3. **Major-version-upgrade the clone to 17.** Aurora's upgrade rewrites the clone's
   *local* volume LSN lineage — which is exactly why the seed LSN must be captured in
   step 2, **before** the upgrade. The captured value stays valid.
4. **On the upgraded 17 clone**, create the subscription **reusing the source slot** and
   advance the origin to the pre-upgrade seed LSN, then enable:
   ```sql
   CREATE SUBSCRIPTION sub CONNECTION '...' PUBLICATION pub
     WITH (copy_data = false, create_slot = false, enabled = false, slot_name = '<slot>');
   SELECT 'pg_'||oid FROM pg_subscription WHERE subname = 'sub';   -- -> pg_<oid>
   SELECT pg_replication_origin_advance('pg_<oid>', '<seed_lsn>'); -- the pre-upgrade LSN
   ALTER SUBSCRIPTION sub ENABLE;
   ```
   Exactly-once: data at/before the seed LSN is already in the clone (no duplicates), and
   the source slot retained the WAL after it (no loss).
5. `validate` → `cutover`, with optional `reverse` (green 17 → blue 16) for rollback
   (see Playbook 4).

Drive the upgrade variant from the **generate-only runbook**
(`guide --init-sync aurora-fast-clone`), slotting the clone's major upgrade in **between
step 2 (seed-LSN capture) and step 4 (CREATE SUBSCRIPTION)**.

### Pattern B — pure logical (simplest)
Provision green directly on Aurora PostgreSQL 17 and follow **Playbook 1** with
`--init-sync copy`. Version-independent; prefer this unless the dataset is too large
for a logical copy.

**Recommendation:** for combined migrate-and-upgrade with huge datasets, Pattern A
gives the fastest seed — capture the seed LSN **before** the clone upgrade, then reuse
the source slot and `pg_replication_origin_advance` to it. Otherwise Pattern B (logical
copy into a 17 green) is the most robust.
