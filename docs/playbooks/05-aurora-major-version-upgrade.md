# Playbook 5 — Aurora major-version upgrade via clone → upgrade → subscribe

**When to use:** you want a near-zero-downtime **major-version upgrade** of an Amazon
Aurora PostgreSQL cluster (e.g. **17 → 18**) with a rollback path. This is the same
shape as **RDS/Aurora managed Blue/Green Deployments for upgrades**: fast-clone the
source, upgrade the clone in place, then set up logical replication so the upgraded
green stays current until you cut over — with reverse replication as rollback insurance.

> Transcribed from an **actual verified run** against a real Aurora PostgreSQL cluster
> (`mma-aurora-pg-cluster`, Serverless v2, **17.7.4 → 18.3**, reached over a VPN). The
> LSNs and results shown are real.

---

## Why clone → upgrade → subscribe

- **Clone** (copy-on-write) is instant regardless of data size and doesn't touch the
  source.
- **Upgrading the clone** (not the source) is safe: if the upgrade or validation fails,
  the source is untouched — just delete the clone.
- **Logical replication 17 → 18** keeps the upgraded green current after the clone
  point, so cutover downtime is just the drain + switch.
- **Reverse (18 → 17)** after cutover keeps the old cluster current for rollback.

## Key facts verified on real Aurora 17/18

- The source already had `rds.logical_replication = on` — **no source reboot needed**.
- `aurora_volume_logical_start_lsn()` **works on Aurora 18** (and 17): it returned
  `0/6188A58` on the clone (captured while still 17). `rds_tools.logical_seed_lsn()`
  does **not** exist on Aurora (RDS-only).
- **Capture the seed LSN on the clone *before* the major upgrade** — the upgrade
  rewrites the volume's LSN lineage, so a post-upgrade value is not a valid resume
  point against the (still-17) source.

## Steps (as run)

**1. Fast-clone the source cluster** (copy-on-write; inherits SG + subnet group; attach
a cluster parameter group that has `rds.logical_replication=1`):
```bash
aws rds restore-db-cluster-to-point-in-time \
  --db-cluster-identifier mma-pgrk-clone \
  --source-db-cluster-identifier mma-aurora-pg-cluster \
  --restore-type copy-on-write --use-latest-restorable-time \
  --db-subnet-group-name mma-rds-dbsubnetgroup \
  --vpc-security-group-ids sg-xxxx
aws rds create-db-instance --db-cluster-identifier mma-pgrk-clone \
  --db-instance-identifier mma-pgrk-clone-1 --db-instance-class db.serverless \
  --engine aurora-postgresql
```

**2. Prepare the source (publication, then slot) and capture the seed LSN on the clone
while still 17:**
```sql
-- on the SOURCE (17), in the database to migrate — publication BEFORE slot:
CREATE PUBLICATION pgrk_up FOR TABLE ...;
SELECT pg_create_logical_replication_slot('pgrk_up','pgoutput');
-- on the CLONE (still 17), capture the seed LSN and SAVE it:
SELECT aurora_volume_logical_start_lsn();     -- e.g. 0/6188A58
```
(`pgreplkit`'s `prepare_source` does the pub-then-slot step; it captures the seed LSN
via the engine-appropriate function.)

**3. Major-version-upgrade the clone to 18** (create an aurora-postgresql18 cluster
parameter group with `rds.logical_replication=1` first):
```bash
aws rds modify-db-cluster --db-cluster-identifier mma-pgrk-clone \
  --engine-version 18.3 --allow-major-version-upgrade --apply-immediately \
  --db-cluster-parameter-group-name pgrk-aurora18-logical
```

**4. Wire logical replication 17 → 18 from the pre-upgrade seed LSN** and enable:
```sql
-- on the upgraded CLONE (18):
CREATE SUBSCRIPTION pgrk_up CONNECTION 'host=<source-private-ip> ... dbname=<db>'
  PUBLICATION pgrk_up
  WITH (copy_data = false, create_slot = false, enabled = false, slot_name = 'pgrk_up');
SELECT 'pg_'||oid FROM pg_subscription WHERE subname='pgrk_up';        -- -> pg_<oid>
SELECT pg_replication_origin_advance('pg_<oid>', '0/6188A58');          -- the seed LSN
ALTER SUBSCRIPTION pgrk_up ENABLE;
```
Post-clone writes on the 17 source now stream to the 18 clone.

**Verified result:**
```
FORWARD 17->18 PASS: clone 50 -> 70 (source 70), distinct=70   # exactly-once
```

**5. Monitor, validate, cut over** (stop writes on 17, drain to lag 0, sync sequences,
validate, switch traffic to the 18 cluster).

**6. Reverse (18 → 17) for rollback insurance** — after cutover, keep the old 17 cluster
current:
```
REVERSE 18->17 PASS: source 70 -> 80 (clone 80)   # writes on 18 reached 17
```
(See Playbook 4 for the reverse flow. `pgreplkit reverse` automates tear-down-forward →
wire-reverse.)

**7. Teardown** when the upgrade is confirmed: drop the subscription/publication/slots,
delete the old 17 cluster (or the clone if you rolled back), and remove the temporary
parameter group.

---

## Networking note (real environment)

- The source already allowed intra-VPC `5432` (`10.1.0.0/16`), so the clone reached the
  source with no security-group change.
- Set the source **private IP** as `advertised_host` so the clone's subscriber connects
  intra-VPC. pgreplkit also uses a **1-second client DNS TTL for RDS/Aurora endpoints**
  (pinning `hostaddr`) so it re-resolves rotated cluster IPs promptly.

## Comparison to managed Blue/Green Deployments

AWS RDS/Aurora Blue/Green Deployments implement this same clone → upgrade → logical-
replication pattern as a managed service. pgreplkit lets you run it yourself when you
need control the managed feature doesn't give — custom slot layouts across many
databases, explicit validation gates, reverse replication on your schedule, or a
generate-only runbook for change control (see Playbook 6).
