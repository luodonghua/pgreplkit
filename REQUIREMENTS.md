# pgreplkit — Requirements

## 1. Overview

**pgreplkit** is a Python command-line tool that automates and de-risks the setup of
PostgreSQL logical replication for blue-green deployment scenarios, with first-class
support for Amazon RDS and Aurora PostgreSQL.

Setting up logical replication by hand is error-prone when a customer has many
databases, many schemas, and many tables, because:

- Logical replication is scoped **per database**, so a cluster with N databases needs
  N publication/subscription/slot sets discovered, managed, and tracked; system and
  empty databases must be skipped, and some databases (e.g. a `postgres` admin DB with
  user tables) are anti-patterns that still need replicating.
- A single apply worker per subscription can bottleneck a busy database; spreading
  tables across multiple slots enables parallel apply but sacrifices cross-slot
  transactional consistency while streaming.
- Every replicated table must have a valid `REPLICA IDENTITY` (a primary key, a
  unique index, or `FULL`). Tables that don't qualify silently break `UPDATE` and
  `DELETE` replication.
- Logical replication does **not** copy schema/DDL, sequences, large objects, or
  **global/cluster-level objects** (roles/users, non-default tablespaces); these must
  be handled out of band.
- An inactive or lagging replication slot causes WAL to accumulate on the source and
  can **fill the source disk → production outage on blue**. This is the single most
  dangerous operational failure mode.
- Logical replication feature availability varies sharply by PostgreSQL major version.
- Operators need a trustworthy signal — not just "lag is zero" — that the green
  environment is complete and correct before they switch traffic, and a safe way to
  **roll back** by reversing replication direction after cutover.

pgreplkit turns this manual, fragile process into a validated, repeatable workflow:
**discover → preflight → global objects → initial data sync → set up → monitor →
validate → cut over → (optional) reverse for rollback**. It supports multiple
initial-sync strategies (built-in copy, RDS snapshot restore, Aurora fast clone) and
multiple slot-allocation strategies (single, per-schema, balanced, manual), and can operate
either by executing changes directly or by generating a review-ready manual runbook
without touching the clusters.

## 2. Goals and Non-Goals

### Goals
- Discover all databases/schemas in a cluster and automate the full lifecycle of
  publications, subscriptions, and replication slots across the in-scope set.
- Let the user control **how many slots per database** via selectable strategies
  (single, per-schema, balanced, manual) to balance parallelism against consistency.
- Catch the full set of blocking conditions **before** any change is made: replica
  identity, target schema existence, version/feature compatibility, source and target
  server prerequisites, encoding/collation parity, and WAL-retention safety.
- Handle objects logical replication ignores: sequences (at cutover), and **global
  objects** (roles/users and non-default tablespaces) needed for the target to
  function.
- Support two provisioning modes — `existing` (both clusters already provisioned; just
  set up replication) and `provision` (create the target to **match the source
  engine**, RDS→RDS or Aurora→Aurora) — and run a prerequisite permission/parameter
  check before any job in both modes.
- Support multiple initial data-sync strategies: built-in logical replication copy,
  RDS snapshot restoration, and Aurora fast clone — each resuming replication from the
  correct consistent LSN.
- Continuously monitor initial-sync progress, replication lag, slot health/WAL
  retention, and apply-worker errors, with warning thresholds.
- Validate data correctness (not just lag) before cutover.
- Provide a safe, ordered cutover flow and a **reverse** operation that flips an
  existing host1→host2 setup into host2→host1 for rollback.
- Offer a generate-only mode that produces a manual runbook (SQL + AWS CLI) without
  executing anything, for users under strict change control.
- Ship as an easily installed Python package with a clean CLI.

### Non-Goals
- It configures/orchestrates PostgreSQL's built-in logical replication; it is **not**
  a replication engine and does not move rows itself.
- Provisioning the target is supported **only as a same-engine match to the source**
  (RDS→RDS or Aurora→Aurora). It does **not** provision or support cross-engine pairs
  (RDS↔Aurora) — see §10.
- It does not author or migrate schema/DDL. It **verifies** target schema exists and
  matches, and (in guide mode) emits the `pg_dump --schema-only` step, but the user
  owns schema creation and ongoing DDL changes.
- It does not manage load balancer / DNS / connection-string switchover at the
  infrastructure layer (it signals readiness; the actual traffic switch is external).
- It does not run steady-state **bidirectional** (active-active) replication; reverse
  is intended as a one-direction rollback path after cutover (see §4.13).
- Initial major-version upgrade orchestration is out of scope beyond enabling
  cross-version logical replication where PostgreSQL/RDS/Aurora themselves allow it.

## 3. Users and Use Cases

**Primary user:** a DBA or platform engineer executing a blue-green migration or
low-downtime maintenance operation on a PostgreSQL cluster (self-managed, RDS, or
Aurora) that may contain many databases and schemas.

Representative use cases:
1. Discover the databases/schemas/tables in a cluster and see which are in scope.
2. Validate whether the in-scope set is eligible for logical replication.
3. Recreate required global objects (roles, tablespaces) on green.
4. Stand up replication across many databases with a chosen slot strategy and
   initial-sync strategy.
5. Watch initial-sync progress, lag, and slot/WAL health until green is caught up.
6. Validate that green's data matches blue, then perform an ordered cutover.
7. Reverse replication (green→blue) after cutover so blue is a rollback target.
8. Tear down replication artifacts cleanly after a successful or aborted migration.

## 4. Functional Requirements

Requirements use EARS-style phrasing. Each is independently testable.

### 4.1 Configuration & Connectivity

- **FR-1** The system SHALL accept connection details for a source and a target
  PostgreSQL cluster via a configuration file and/or environment variables.
- **FR-2** The system SHALL never store plaintext passwords in logs or output, and
  SHALL support standard PostgreSQL credential sources (`PGPASSWORD`, `.pgpass`,
  connection URIs) as well as retrieval from a secrets source (e.g., AWS Secrets
  Manager) where configured.
- **FR-3** WHEN a connection to source or target fails, the system SHALL report the
  affected host/database and the underlying error without leaking credentials.

### 4.2 Cluster Topology: Database & Schema Discovery

- **FR-4** The system SHALL provide a `discover` capability that enumerates all
  databases in the source cluster and, for each, its schemas and tables, and reports
  which are **in scope** versus **skipped** and why.
- **FR-5** The system SHALL **skip system/maintenance databases** by default,
  including `template0`, `template1`, `rdsadmin`, and any database that is a template
  (`datistemplate = true`) or disallows connections (`datallowconn = false`). Skips
  SHALL be reported, and the skip list SHALL be overridable by the user.
- **FR-6** The system SHALL **skip databases that contain no user tables** by default
  (reporting them as skipped), because there is nothing to replicate.
- **FR-7** WHEN the `postgres` (default admin/maintenance) database contains user
  tables, the system SHALL emit a prominent warning that placing application tables in
  the `postgres` database is not a best practice, but SHALL still include it and set
  up replication for it unless the user excludes it.
- **FR-8** The system SHALL support **multiple schemas per database**, including all
  non-system schemas by default, and SHALL allow include/exclude filtering by
  database, schema, and table (patterns permitted).
- **FR-9** The system SHALL support explicit database selection (list or pattern) that
  overrides discovery, and SHALL always apply the system-database skip rules unless
  explicitly forced.

### 4.3 Slot Allocation Strategy

The number of subscriptions/slots per database determines apply parallelism. Each
subscription has one slot and (pre-PG16) one apply worker; multiple subscriptions in a
database give parallel apply across independent WAL streams.

- **FR-10** The system SHALL let the user choose a **slot-allocation strategy per
  database** (with a cluster-wide default), supporting at least:
  - `single` — one publication/subscription/slot covering all in-scope tables in the
    database.
  - `per-schema` — one publication/subscription/slot per in-scope schema.
  - `balanced` (**default**) — FK-affinity grouping plus weighted bin-packing of all
    in-scope tables across a user-chosen `N` slots (see FR-11).
  - `manual` — an explicit, user-supplied slot→table mapping (see FR-12).

  The strategy SHALL be selectable per database and cluster-wide (`--slots`, with `N`
  via `--n`). This supersedes an earlier "top-k" idea, which is just the degenerate
  case of `balanced` where the heaviest tables are isolated and the remainder is not
  spread; `balanced` is preferred because it also distributes the long tail.
- **FR-11** The `balanced` strategy SHALL distribute tables to equalize *apply load*,
  not table count, using the following engine:
  - **Weight metric:** per-table write activity from source statistics
    (`pg_stat_all_tables`: `n_tup_ins + n_tup_upd + n_tup_del`), read **point-in-time
    by default**, optionally weighted by average row width (apply cost ≈ bytes) rather
    than row count alone. A sampling window (two reads spaced in time) MAY be offered
    as a non-default refinement.
  - **FK-affinity grouping:** tables connected by foreign keys SHALL be treated as a
    single unit (connected components of the FK graph) and kept in the **same** slot,
    so referential integrity is preserved within a stream and cross-slot FK violations
    are avoided.
  - **Weighted bin-packing:** the resulting groups SHALL be packed into `N` slots
    using a greedy longest-processing-time assignment (heaviest group to the
    currently-least-loaded slot) to balance estimated per-slot load.
  - **Partition spreading (modifier):** for a large partitioned table that is itself a
    bottleneck, the system SHALL optionally spread its partitions across slots using
    `publish_via_partition_root = false`, so a single hot partitioned table can be
    parallelized (opt-in, and mutually exclusive with publishing that table via its
    root). *Status:* implemented — the planner discovers leaf partitions from the
    catalog (`pg_partition_tree`), weights them by per-leaf write activity, and packs
    them across slots when `--spread-partitions` is set; otherwise a partitioned table
    is published as one unit via its root (`publish_via_partition_root = true`).
  The system SHALL report the resulting layout (slot → tables, with estimated per-slot
  weight) and SHALL allow the user to override individual assignments.
- **FR-12** The `manual` strategy SHALL let the user declare an explicit per-database
  slot→table mapping, and SHALL enforce and support the following:
  - **Partition rule:** the mapping MUST assign every in-scope table to exactly one
    slot. A catch-all pattern (`"*"`) MAY capture unlisted tables; if in-scope tables
    remain unassigned and no catch-all exists, preflight SHALL fail listing them
    (creating a leftover slot only with an explicit override).
  - **Precedence:** explicit table name > glob (e.g., `audit.*`) > catch-all.
  - **Validation:** preflight SHALL resolve and print the final layout and SHALL flag
    tables matched by more than one rule (ambiguous), tables listed that are not in
    scope or do not exist, empty slots, and cross-slot FK edges.
  - **Plan-to-seed workflow:** the system SHALL provide a `plan` command that emits
    the computed `balanced` layout as an editable manual mapping (e.g., YAML), so
    users tune a generated plan rather than authoring it from scratch, then feed it
    back as `manual`.
  - **Reconciliation:** WHEN new in-scope tables appear later (FR-44), `manual` SHALL
    route them via the catch-all if present, otherwise flag them as unassigned for the
    user to place; `balanced` SHALL re-pack. The system SHALL document that `manual`
    trades adaptability for control.
- **FR-13** The system SHALL bound and safety-check multi-slot allocation on two axes:
  - **Decode-cost cap & headroom:** because **each logical slot independently decodes
    the entire source WAL stream** and filters to its own tables, `N` slots impose
    roughly `N×` logical-decoding CPU and reorder-buffer memory on the **source
    (blue)**. The system SHALL check the total slot count against source
    `max_wal_senders` / `max_replication_slots` and target worker/slot headroom (ties
    to FR-23/FR-26), SHALL enforce a configurable **maximum slot count** and warn when
    the requested `N` approaches or exceeds it (explaining the publisher-side cost),
    and SHALL report the total slot count per database and cluster-wide.
  - **Cross-slot consistency caveat:** splitting a database across multiple slots does
    **not** preserve cross-slot transactional consistency while streaming — a
    transaction writing tables in different slots may be applied at different times
    (torn), and cross-slot foreign-key relationships may be transiently violated. The
    system SHALL note that (a) final state converges once writes stop and lag reaches
    zero at the drain step, (b) `session_replication_role = replica` on the target
    (FR-51) prevents FK/trigger enforcement from blocking apply, and (c) FK-affinity
    grouping (FR-11) keeps referentially related tables together for `balanced`. WHERE
    cross-slot FK dependencies remain (e.g., under `manual` or `per-schema`), the
    system SHALL warn and SHOULD recommend keeping FK-related tables in the same slot.

### 4.4 Preflight Validation

#### Replica identity & unreplicated objects
- **FR-14** The system SHALL provide a `preflight` command that inspects every
  in-scope table and reports its `REPLICA IDENTITY` status.
- **FR-15** WHEN a table lacks a primary key and has no usable unique index and is not
  set to `REPLICA IDENTITY FULL`, the system SHALL flag it as **not replication-safe**
  for `UPDATE`/`DELETE`.
- **FR-16** For each flagged table, the system SHALL emit at least one concrete
  remediation option (add primary key, set `REPLICA IDENTITY FULL`, or exclude from
  the publication), and SHALL NOT present them as equivalent: it SHALL flag the
  **cost of `REPLICA IDENTITY FULL`** — it logs the *entire old row* in WAL on the
  publisher (more WAL + more decode cost, compounding the multi-slot decode-cost
  concern in FR-13) and forces a **sequential scan per changed row** on the subscriber
  when no suitable index exists. Adding a primary key / unique index is preferred where
  feasible.
- **FR-17** The system SHALL detect and warn about objects not carried by logical
  replication: sequences, large objects (`pg_largeobject`), and (informationally) that
  DDL is not replicated. For databases that actually use **large objects**, the system
  SHALL surface this as a data-loss risk (LOs would be silently absent on green),
  SHALL count/compare `pg_largeobject_metadata` during `validate` (FR-61a), and in
  guide mode SHALL note that LOs need a separate `pg_dump`/migration step.
- **FR-17a** Preflight SHALL classify every in-scope relation by kind and screen out
  those that **cannot be published**: attempting to replicate **views, materialized
  views, or foreign tables raises an error**, and **unlogged** and **temporary** tables
  are not replicated (their data will silently not appear on the target). Generated
  columns are not replicated before PG18. The system SHALL block on relation kinds that
  would error, warn on kinds that would silently not replicate, and offer to exclude
  them from scope.

#### Target schema pre-creation (blocking prerequisite)
- **FR-18** The system SHALL verify, for the `copy` and `pre-seeded` strategies, that
  every in-scope table **already exists on the target** with compatible column
  definitions (name, type, nullability, ordinal position), and SHALL treat missing or
  incompatible target tables as a blocking error rather than a warning.
- **FR-19** WHEN target tables are missing, guide mode SHALL emit the
  `pg_dump --schema-only` (and matching `pg_restore`/`psql`) steps needed to
  pre-create the schema on green.

#### Version & feature compatibility
- **FR-20** The system SHALL detect the PostgreSQL major version of both source and
  target and SHALL gate features by version, at minimum: `TRUNCATE` replication (11+),
  streaming of in-progress transactions (14+), two-phase commit (14/15+), row filters,
  column lists, and `FOR TABLES IN SCHEMA` publications (15+), and parallel apply /
  `origin = none` (16+).
- **FR-21** The system SHALL define and enforce a **minimum supported version** and
  SHALL refuse (or clearly warn) when a requested feature is unavailable on either
  endpoint's version.

#### Encoding / collation parity
- **FR-22** The system SHALL compare server/database encoding and `LC_COLLATE` /
  `LC_CTYPE` between source and target and SHALL warn when they differ, because a
  collation mismatch can corrupt text ordering and break uniqueness on the target.

#### Source-side server prerequisites
- **FR-23** The system SHALL verify source prerequisites, including logical WAL being
  enabled and sufficient `max_replication_slots`, `max_wal_senders`, and
  `max_worker_processes` headroom **for the total slot count implied by the chosen
  slot-allocation strategy**, and report any that are insufficient.
- **FR-24** On **RDS/Aurora sources**, the system SHALL key the logical-WAL check off
  the managed parameter (`rds.logical_replication = 1`, a **static** parameter that
  requires a reboot to take effect) in the relevant parameter group, rather than
  expecting the user to set `wal_level` directly, and SHALL detect the case where the
  parameter is set but a reboot is still pending.
- **FR-25** The system SHALL check that the connecting role holds the privileges
  required on the source to create publications and read the intended tables, and on
  RDS/Aurora SHALL name the managed roles involved (e.g., `rds_replication`,
  `rds_superuser`).

#### Target-side server prerequisites
- **FR-26** The system SHALL verify **target/subscriber** prerequisites, including
  `max_logical_replication_workers`, `max_worker_processes`,
  `max_sync_workers_per_subscription`, and `max_replication_slots` headroom **for the
  total subscription count**, and report any that are insufficient.
- **FR-27** The system SHALL verify network reachability from **target → source**
  (the subscription dials out to the publisher), including reporting likely security
  group / firewall / `pg_hba.conf` blockers.
- **FR-28** The system SHALL verify the connecting role can create subscriptions and
  SHALL account for the version/engine reality: `pg_create_subscription` exists only on
  **PG16+**; on **self-managed PG < 16 there is no least-privilege path — subscription
  creation requires superuser**. On RDS/Aurora, membership in `rds_replication` /
  `rds_superuser` (or `pg_create_subscription` on PG16+) is required. The system SHALL
  name the applicable roles/privileges for the detected version and engine, and SHALL
  not imply a least-privilege option exists where it does not.

#### WAL retention safety (outage prevention)
- **FR-29** The system SHALL evaluate and report WAL-retention safety, recognizing the
  **two opposite failure modes of `max_slot_wal_keep_size`**:
  - **Unbounded (`-1`, the default):** slots are never invalidated, so an inactive or
    lagging slot retains WAL without limit → **source disk fills → production
    outage**. The system SHALL warn that retention is unbounded and recommend disk-
    space monitoring/alarms.
  - **Bounded (a size is set):** a slot whose retained WAL exceeds the bound is dropped
    as **`lost`**, silently breaking replication and forcing a full resync. The system
    SHALL warn about lost-slot risk relative to the expected lag/seed-retention window.
  On RDS/Aurora, the system SHALL point to CloudWatch `OldestReplicationSlotLag`,
  `ReplicationSlotDiskUsage`, and `FreeStorageSpace` for proactive alarms.

#### Output
- **FR-30** The system SHALL support a machine-readable output mode (JSON) for
  preflight/discover results in addition to human-readable output.
- **FR-31** The `preflight` and `discover` commands SHALL be strictly read-only and
  make no changes to either cluster.

### 4.5 Global Objects (roles / users / tablespaces)

Logical replication does not replicate cluster-level objects. Missing roles or
tablespaces cause `CREATE SUBSCRIPTION`/apply or schema restore to fail on green.

- **FR-32** The system SHALL detect the **roles/users** referenced by in-scope objects
  (owners and grantees of tables, schemas, and sequences) and SHALL report which of
  those roles are missing on the target.
- **FR-33** The system SHALL be able to reproduce the required roles and their
  grants/memberships on the target (equivalent to `pg_dumpall --roles-only` /
  `--globals-only`, filtered to what is needed), and in guide mode SHALL emit the
  corresponding `CREATE ROLE` / `GRANT` SQL. On RDS/Aurora it SHALL account for
  managed-role constraints (e.g., no plaintext superuser, password/role handling via
  the managed API) and SHALL NOT attempt operations the managed engine forbids.
  Because role passwords cannot be recovered from the source in a managed environment
  (`pg_dumpall` cannot dump unknown passwords), WHEN a role must be created on the
  target and its password is unavailable, the system SHALL create the role with a
  **strong, randomly generated password** and SHALL record it securely for DBA
  reference (written to a protected credentials file / secret and referenced by role
  name, never emitted to normal logs — ties to FR-2). The system SHALL clearly report
  which roles were created with generated passwords so the DBA can rotate/reset them.
- **FR-34** The system SHALL detect **non-default tablespaces** used by in-scope
  objects and SHALL report them, because tablespaces are not replicated and must exist
  on the target. In guide mode it SHALL emit the `CREATE TABLESPACE` steps, and SHALL
  clearly note that on RDS/Aurora user-defined tablespaces are restricted/unsupported,
  requiring objects to be mapped to the default tablespace instead.
- **FR-35** Global-object handling SHALL be reported as a distinct preflight/setup
  phase, and unmet role/tablespace prerequisites SHALL be treated as blocking for the
  affected objects.

### 4.6 Setup / Orchestration

- **FR-36** The system SHALL provide a `setup` command that creates the required
  publications on the source and subscriptions (with slots) on the target for all
  in-scope databases, honoring the chosen slot-allocation strategy (FR-10).
- **FR-37** The system SHALL make publication content configurable and reported,
  including the published operation set (`insert`, `update`, `delete`, `truncate`) and
  the `publish_via_partition_root` option, and SHALL default to safe, explicit values.
- **FR-38** The system SHALL run preflight validation automatically before setup and
  SHALL refuse to proceed if blocking issues exist (replica identity, non-replicable
  relation kinds, missing target schema, missing global objects, version/feature
  incompatibility, insufficient prerequisites), unless the user explicitly overrides.
  The system SHALL distinguish **correctness blocks** (e.g., missing replica identity,
  which silently breaks `UPDATE`/`DELETE` — a data-correctness loss) from **capacity
  blocks** (e.g., insufficient slot headroom): overriding a *correctness* block SHALL
  require a distinct, louder confirmation (a separate flag/prompt, not a blanket
  `--force`), and every override SHALL be recorded in the manifest with what was
  bypassed and when.
- **FR-39** The system SHALL apply a configurable `statement_timeout` and
  `lock_timeout` to potentially long/locking operations (e.g., `CREATE PUBLICATION`,
  `CREATE SUBSCRIPTION ... copy_data = true`) so a stuck operation cannot stall the
  production source indefinitely.
- **FR-40** The system SHALL provide a `--dry-run` mode that prints every SQL
  statement it would execute without applying it.
- **FR-41** The system SHALL be idempotent: re-running `setup` SHALL detect existing
  publications/subscriptions/slots. It SHALL NOT silently adopt an existing object by
  name alone — it SHALL **verify the existing object matches the computed plan** (e.g.,
  a publication's table set and `publish` operations, a subscription's slot/options)
  and, when the existing object **diverges** from the plan, report a **conflict** and
  refuse to proceed (rather than producing a setup that doesn't match the plan) unless
  the user explicitly resolves it.
- **FR-42** WHEN setup fails partway through a multi-database run, the system SHALL
  report which databases/slots succeeded and which failed, and SHALL record enough
  state to identify and resume partial work (see FR-74, manifest).
- **FR-43** The system SHALL name created objects using a consistent, discoverable
  naming convention (prefixed, encoding database and slot index) so they can be found
  and cleaned up later.
- **FR-44** The system SHALL support **reconciling schema changes after setup**: when
  new in-scope tables appear on the source, it SHALL be able to
  `ALTER PUBLICATION ... ADD TABLE` (into the appropriate slot per the strategy) and
  `ALTER SUBSCRIPTION ... REFRESH PUBLICATION`, and warn that newly added tables must
  already exist on the target.

#### Subscription connection secret handling
- **FR-45** The system SHALL document and manage the fact that `CREATE SUBSCRIPTION`
  stores the source `conninfo` (including any password) in the target catalog
  (`pg_subscription`), where it is readable by privileged roles and can appear in
  dumps. The system SHALL prefer a dedicated least-privilege replication user and
  SHALL offer guidance (and where possible a mechanism) to avoid embedding long-lived
  secrets in the subscription.

### 4.7 Initial Data Synchronization

- **FR-46** The system SHALL support selecting an initial-sync strategy per run, with
  at least:
  - `copy` — subscription created `WITH (copy_data = true)`; the logical replication
    workers perform the initial data load.
  - `snapshot-restore` — the target (green) is provisioned by restoring an RDS
    snapshot of the source, then replication resumes from the seed LSN.
  - `aurora-fast-clone` — the target is provisioned via an Aurora fast
    (copy-on-write) clone of the source cluster, then replication resumes from the
    seed LSN.
  - `none` / `pre-seeded` — data already loaded by other means; the tool only wires
    up replication.
- **FR-47** WHEN the `copy` strategy is selected, the system SHALL create
  subscriptions with `copy_data = true`, SHALL expose the initial table-sync
  parallelism (`max_sync_workers_per_subscription`) as a tunable, and SHALL monitor
  initial-copy progress via `pg_subscription_rel` / `pg_stat_subscription` until all
  tables reach the ready/streaming state. Where available, the system SHOULD surface
  finer-grained progress within a large table's initial copy (e.g.,
  `pg_stat_progress_copy` on the subscriber) for long-running copies.
- **FR-48** WHEN a physical-seed strategy (`snapshot-restore` or `aurora-fast-clone`)
  is selected, the system SHALL create subscriptions with `copy_data = false` and
  SHALL establish replication from the **consistent seed LSN** so that changes made
  after the snapshot/clone point are replayed exactly once, without duplicating or
  skipping the seeded data.
- **FR-49** Physical-seed strategies are **same-engine only** — the target engine
  matches the source (RDS→RDS or Aurora→Aurora); cross-engine physical seeds
  (RDS↔Aurora) are out of scope (see §10 for the rationale). For the supported
  same-engine pairs, LSN continuity is preserved, so the seed LSN is captured directly
  on the restored/cloned target:
  - **RDS → RDS** (snapshot restore): `rds_tools.logical_seed_lsn()` on the restored
    target.
  - **Aurora → Aurora** (fast clone or snapshot restore):
    `aurora_volume_logical_start_lsn()` on the clone/restore.

  The system SHALL detect the engine (FR-15/§8), select the matching function, and
  refuse to proceed if it cannot determine a valid seed LSN. If a user presents a
  cross-engine **customer-provisioned** pair, the system SHALL refuse the physical-seed
  strategies and direct them to `copy`.
- **FR-50** For physical-seed strategies, the system SHALL enforce, execute (or in
  guide mode emit) the exact ordered resume sequence that guarantees **exactly-once at
  the seed boundary**:
  1. On the source, **before** the snapshot/clone, create the publication and then the
     logical replication slot, in that order: `CREATE PUBLICATION` first, then
     `pg_create_logical_replication_slot('<slot>', 'pgoutput')`. Ordering is
     load-bearing: pgoutput logical decoding replays from the slot's `restart_lsn`, so
     the publication must already exist at that point (otherwise decoding fails with
     "publication does not exist"). Creating the slot before the snapshot retains WAL
     from a point at or before the seed.
  2. Take the snapshot / create the clone and provision the target (same engine as the
     source), then capture the seed LSN on the target per FR-49.
  3. (Publication already exists from step 1.)
  4. On the target, `CREATE SUBSCRIPTION ... PUBLICATION ... WITH (copy_data = false,
     create_slot = false, enabled = false, connect = true, slot_name = '<slot>')`.
  5. Resolve the subscription's replication-origin name as `'pg_' || oid` from
     `pg_subscription`, then `SELECT pg_replication_origin_advance('pg_<oid>',
     '<seed_lsn>')` to move the origin to the captured seed LSN.
  6. `ALTER SUBSCRIPTION ... ENABLE` to begin streaming exactly at the seed point.

  The system SHALL treat this sequence as load-bearing: skipping the origin advance
  causes the subscriber to replay from the slot's pre-seed `confirmed_flush_lsn` and
  **duplicate** rows already in the seed (or, with a wrong-direction LSN, **lose**
  rows). An Aurora fast clone is copy-on-write and shares the source timeline
  (clone → promote); this SHALL be surfaced in guide output.
- **FR-51** The system SHALL document and, where configured, help apply
  `session_replication_role = replica` on the apply side so that target triggers and
  foreign-key constraints do not fire during apply and block the worker. (Also
  mitigates cross-slot FK issues from FR-13.)
- **FR-52** The system SHALL support two **provisioning modes**, selectable per run:
  - `existing` (bring-your-own) — the customer has already provisioned both the source
    and target clusters; the tool only sets up replication (no infrastructure is
    created). This is the default.
  - `provision` — the customer has a source but no target; the tool provisions the
    target infrastructure **to match the source engine** (RDS→RDS or Aurora→Aurora,
    same major version unless a cross-version target is explicitly requested and
    supported). The tool SHALL NOT create a cross-engine target.

  Provisioning creates billable AWS resources, so `provision` mode SHALL be explicit,
  SHALL require confirmation (NFR-3), and SHALL support `--dry-run`/guide output that
  emits the exact AWS CLI/console steps instead of executing them. Snapshot/clone
  actions used by the physical-seed strategies (FR-46) are part of this mode.
- **FR-52a** Before starting any job, in **both** provisioning modes, the system SHALL
  run a **prerequisite check** covering necessary permissions and parameters, and SHALL
  refuse to start if unmet:
  - **`existing` mode:** connectivity to source and target; DB parameters and
    server prerequisites (FR-23/FR-24/FR-26); managed roles/privileges
    (FR-25/FR-28); target→source reachability (FR-27).
  - **`provision` mode:** additionally, the **AWS/IAM permissions** required to create
    the target (e.g., `rds:CreateDBInstance`/`CreateDBCluster`,
    `rds:RestoreDBInstanceFromDBSnapshot` / `RestoreDBClusterFromSnapshot`,
    `CreateDBSnapshot`, parameter-group and subnet-group access), relevant **service
    quotas**, and networking (subnet group, security groups, KMS key access).
  The prerequisite check SHALL be read-only, report each item as ok/warn/block with
  remediation, and be available as machine-readable output (FR-30) for pipeline gating.
- **FR-53** The system SHALL report, per table, whether initial sync has completed, is
  in progress, or failed, regardless of the strategy chosen.

### 4.8 Monitoring

- **FR-54** The system SHALL provide a `status` command that reports, per database and
  per slot: subscription state, initial-sync progress, apply lag, and slot state. The
  **authoritative apply lag** SHALL be measured in **bytes behind** on the source, as
  `pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)` for the slot in
  `pg_replication_slots` (equivalently `write_lag`/`flush_lag`/`replay_lag` from
  `pg_stat_replication` on the publisher), because that reflects committed WAL not yet
  confirmed applied. Receiver-side `pg_stat_subscription` timestamps MAY be shown as
  supplementary but SHALL NOT be the primary lag signal.
- **FR-55** The system SHALL monitor and report **replication-slot / WAL-retention
  health**, including `pg_replication_slots.wal_status`
  (`reserved` / `extended` / `unreserved` / `lost`), retained-WAL bytes, `safe_wal_size`
  where available, and whether the slot is `active`, and SHALL emit a warning when
  retained WAL crosses a configurable threshold or the slot goes inactive, and an error
  when a slot is `lost` (consistent with the two failure modes in FR-29).
- **FR-56** The system SHALL surface apply-side errors reported by PostgreSQL (e.g.,
  subscription worker errors in `pg_stat_subscription`/logs) in the status output.
  Because a schema migration run on the source **while replication is live** (an added/
  dropped/retyped column) will break apply and can wedge a slot, `status`/`watch` SHALL
  **proactively re-compare** source and target schema for in-scope tables and warn on
  drift *before* it stalls a worker, and the runbook SHALL call out this hazard.
- **FR-57** The system SHALL provide a `watch` mode that refreshes status on an
  interval until interrupted or until all databases/slots reach a caught-up threshold.

### 4.9 Apply-Side Error & Conflict Handling

- **FR-58** The system SHALL support creating subscriptions with `disable_on_error`
  (PG15+) where available, so a failing subscription disables itself instead of
  looping, and SHALL report when this option is unavailable.
- **FR-59** The system SHALL provide an operation to skip a specific failing
  transaction on a subscription (`ALTER SUBSCRIPTION ... SKIP (lsn = ...)`), gated
  behind explicit confirmation because skipping loses data.
- **FR-60** The system SHALL warn that a stuck apply worker blocks its entire
  subscription (and, under multi-slot strategies, only that slot's tables), and SHALL
  surface which slot/subscription is affected.

### 4.10 Pre-Cutover Data Validation

- **FR-61** The system SHALL provide a `validate` command that compares source and
  target beyond lag, with a **user-selectable depth**:
  - `none` — object/count checks only (see FR-61a), no per-row comparison;
  - `sampled` — per-table row counts plus a sampled checksum;
  - `full` — a full-table checksum, opt-in **per table** (expensive).

  The default SHALL be `sampled`. The depth is optional and independent of the
  object-count checks below.
- **FR-61a** Regardless of the selected checksum depth (including `none`), the system
  SHALL compare **object counts** between source and target for the in-scope set —
  tables, sequences, indexes, and other relevant relation kinds — and SHALL also
  compare **global objects**, in particular the set of roles/users, reporting any
  present on the source but missing on the target (ties to FR-32/FR-33).
- **FR-62** The `validate` command SHALL clearly report per-table pass/fail and
  object/global-object discrepancies, and SHALL return a non-zero exit code when any
  in-scope table or object-count check fails, so it can gate cutover.

### 4.11 Cutover Orchestration

- **FR-63** The system SHALL provide a `sync-sequences` command that reads sequence
  values from the source and applies them to the target (sequences are not carried by
  logical replication).
- **FR-64** The system SHALL provide a composite readiness gate (`ready`) that passes
  **only when all** of the following hold for every in-scope database and slot:
  initial sync complete, no unresolved apply-worker errors, slot active and not
  `lost`, and apply lag within a user-defined threshold measured in **bytes behind**
  off the slot's `confirmed_flush_lsn` on the source (per FR-54). It SHALL return a
  non-zero exit code on failure for pipeline gating.
- **FR-65** The system SHALL provide a `cutover` orchestration command (and, in guide
  mode, an equivalent ordered runbook) that enforces the safety-critical ordering:
  1. quiesce/stop writes on blue (or confirm the caller has done so),
  2. wait for final drain until lag → 0 across **all** slots,
  3. run `sync-sequences` (**must** occur after writes stop on blue, or green
     sequences will collide),
  4. run `validate`,
  5. signal readiness for the external traffic switch.
  The command SHALL refuse to proceed past drain if any slot's lag is nonzero and
  SHALL refuse to signal readiness if validation fails.

### 4.12 Teardown

- **FR-66** The system SHALL provide a `teardown` command that drops the
  subscriptions, slots, and publications it created (all slots per database), across
  all in-scope databases.
- **FR-67** Because teardown is destructive, the system SHALL require explicit
  confirmation (interactive prompt or `--yes`) before dropping anything, and SHALL
  support `--dry-run`.
- **FR-68** The system SHALL warn if it detects replication artifacts that do not
  match its naming convention/manifest, and SHALL NOT drop objects it did not create
  unless explicitly told to.
- **FR-69** The system SHALL detect and help remediate an orphaned/inactive slot left
  on the source after subscription removal, since such a slot continues to retain WAL.

### 4.13 Reverse / Rollback (direction swap)

Blue-green's core value is safe rollback. After cutover, blue is stale; reversing
replication keeps blue current so it can be switched back to.

Reverse is performed **from an in-sync state**: the operator stops/removes the forward
direction while it is caught up (lag zero, per the cutover drain), at which point the
two clusters are byte-for-byte equivalent for the in-scope data. Because of this,
reverse requires **no re-seed** — the reverse subscription is created with initial-sync
`none` (`copy_data = false`) and simply begins streaming the writes that occur on the
new source (former green) after the swap. This is why there is no data-stability issue:
the operator controls the stop-forward / start-reverse transition from a consistent
point.

- **FR-70** The system SHALL provide a `reverse` command that flips an existing
  pgreplkit-managed setup from **host1 → host2** into **host2 → host1**, reusing the
  same in-scope databases, schemas, and slot-allocation configuration in the opposite
  direction. The reverse direction SHALL use initial-sync `none`, relying on the
  in-sync swap point (both clusters equivalent when the forward direction was stopped
  at zero lag); it SHALL NOT re-copy existing data.
- **FR-71** Before reversing, the system SHALL run preflight in the reverse direction
  (the former target becomes the new source and must satisfy replica-identity, logical
  WAL, prerequisites, and — for the new target — schema/global-object requirements),
  and SHALL verify the forward direction was stopped at zero lag (in-sync) before
  wiring up the reverse direction, so no post-swap writes are missed.
- **FR-72** To avoid a **replication loop**, the system SHALL by default require that
  the forward direction is torn down (or already inactive) before establishing the
  reverse direction, and SHALL refuse to create bidirectional replication unless the
  user explicitly opts in on a version that supports loop avoidance
  (`origin = none`, PG16+), in which case it SHALL configure that safeguard.
- **FR-73** The system SHALL use its manifest (FR-74) to identify the existing
  forward-direction artifacts when reversing, and SHALL record the new direction so
  that subsequent `status`/`teardown` operate on the reverse setup.

### 4.14 State / Manifest

- **FR-74** The system SHALL maintain a local **manifest** of objects it creates
  (publications, subscriptions, slots — per database and slot index — with the
  direction, timestamps, chosen options/strategy, and initial-sync strategy), and
  SHALL use the manifest — not naming convention alone — to drive `status`, resume,
  `reverse`, and `teardown`, so operations remain robust even if naming collides or
  partial failures occur.

### 4.15 Guide / Manual Mode (generate-only, no execution)

Some users cannot or will not let a tool connect and execute changes against their
clusters (change-control policies, break-glass runbooks, air-gapped review). pgreplkit
SHALL support a mode that produces a complete, human-readable runbook plus exact SQL
and CLI commands, without executing anything.

- **FR-75** The system SHALL provide a guide/generate mode (a `guide` command or a
  global `--generate-only` flag) that, for any operation (discover, preflight, global
  objects, schema pre-creation, setup with the chosen slot strategy, each initial-sync
  strategy, validate, cutover, reverse, teardown), produces the ordered set of steps
  and the exact SQL the user would run manually.
- **FR-76** In guide mode, the system SHALL NOT execute any statement that modifies
  either cluster. Read-only inspection MAY be performed to tailor the guide to the
  user's actual topology; if the user declines any connection, the system SHALL still
  emit a parameterized/templated guide from configuration alone.
- **FR-77** The generated output SHALL include, where relevant: the database/schema
  scope and skip decisions, the per-database slot layout (including balanced/manual
  slot assignments and any partition spreading), prerequisite checks (including the RDS `rds.logical_replication`
  parameter-group change + reboot, and target-side worker/network checks), global
  objects (`CREATE ROLE`/`GRANT`, `CREATE TABLESPACE`), `pg_dump --schema-only` schema
  pre-creation, `CREATE PUBLICATION` / `CREATE SUBSCRIPTION` per database/slot,
  replica-identity remediation SQL, the chosen initial-sync procedure (including RDS
  snapshot / Aurora fast-clone AWS CLI steps and the `aurora_volume_logical_start_lsn()`
  / `logical_seed_lsn()` capture), verification/validation queries, the ordered cutover
  steps, the reverse-direction procedure, and teardown SQL.
- **FR-78** The generated guide SHALL be emitted in at least one durable, shareable
  format (Markdown runbook and/or `.sql` script files) so it can be attached to a
  change ticket or reviewed offline.
- **FR-79** The generated commands SHALL use the tool's consistent object naming
  convention so that a later `status`/`teardown` run can recognize artifacts created
  manually from the guide.
- **FR-80** The guide SHALL clearly annotate any step performed outside SQL (RDS
  parameter change + reboot, taking a snapshot, creating an Aurora clone, creating
  tablespaces/roles via managed APIs) and every ordering dependency between steps.

## 5. Non-Functional Requirements

- **NFR-1 (Packaging)** The tool SHALL be installable via `pip`/`pipx` and expose a
  single console entry point `pgreplkit`.
- **NFR-2 (Python version)** The tool SHALL support currently maintained Python
  versions (3.10+).
- **NFR-3 (Safety)** All commands that modify state SHALL support `--dry-run`, and
  destructive commands SHALL require explicit confirmation.
- **NFR-4 (Concurrency)** Multi-database and multi-slot operations SHALL run in
  parallel with a configurable concurrency limit, while producing deterministic,
  per-database/per-slot reporting; initial table-sync parallelism SHALL be tunable
  (`max_sync_workers_per_subscription`).
  *v0.1 status:* `setup` runs databases/slots **sequentially** from the orchestrating
  thread (the manifest single-writer funnel is in place); cross-database parallel
  execution and a `--concurrency` knob are planned but not yet active. Initial
  table-sync parallelism (a server-side setting) is already effective.
- **NFR-5 (Observability)** The tool SHALL support adjustable log verbosity and a
  structured (JSON) output mode for integration into pipelines.
- **NFR-6 (Exit codes)** Commands SHALL use meaningful exit codes (0 = success,
  non-zero = validation failure or error) so they can gate CI/CD pipelines.
- **NFR-7 (Least privilege)** The tool SHALL document the minimum PostgreSQL
  privileges required for each operation — including the RDS/Aurora managed roles
  (`rds_replication`, `rds_superuser`, `pg_create_subscription` on PG16+) — and SHALL
  prefer read-only access where a command does not need to write. It SHALL state
  clearly that **self-managed PG < 16 requires superuser to create subscriptions**
  (no least-privilege alternative exists there).
- **NFR-8 (Idempotency & recoverability)** Re-running any command after a failure
  SHALL be safe and SHALL converge toward the desired state, aided by the manifest.
- **NFR-9 (No data exfiltration)** The tool SHALL only connect to the source/target
  clusters and configured AWS APIs the user specifies, and SHALL make no other
  outbound network calls.
- **NFR-10 (Timeouts)** The tool SHALL apply configurable statement/lock timeouts to
  operations run against production to avoid stalling the source. It SHALL also
  surface/advise on `wal_sender_timeout` (publisher) and `wal_receiver_timeout`
  (subscriber), since their defaults can drop a slow or high-latency initial copy, and
  SHALL allow tuning them for long copies.

## 6. Suggested CLI Surface

```
pgreplkit discover         # enumerate DBs/schemas/tables; show in-scope vs skipped
pgreplkit plan             # compute & emit the balanced slot layout as editable YAML (seed for manual)
pgreplkit preflight        # read-only eligibility & prerequisite report (source + target)
pgreplkit globals          # detect/recreate roles & tablespaces on target
pgreplkit setup            # create pubs/subs/slots (supports --dry-run)
                           #   --init-sync {copy|snapshot-restore|aurora-fast-clone|none}
                           #   --slots {single|per-schema|balanced|manual} [--n N]
                           #   --slot-map FILE        # required for --slots manual
                           #   --spread-partitions    # spread hot partitioned table across slots
                           #   --max-slots N          # decode-cost cap
                           #   --publish {insert,update,delete,truncate}
                           #   --publish-via-partition-root
pgreplkit refresh          # ALTER PUBLICATION ADD TABLE + REFRESH SUBSCRIPTION (new tables)
pgreplkit status           # replication + initial-sync + slot/WAL health (per db/slot)
pgreplkit watch            # continuous status until caught up / interrupted
pgreplkit validate         # per-table row-count / checksum comparison
pgreplkit sync-sequences   # copy sequence values source -> target
pgreplkit ready            # composite pass/fail gate (sync+errors+slot+lag)
pgreplkit cutover          # ordered: quiesce -> drain -> sequences -> validate -> signal
pgreplkit reverse          # flip host1->host2 into host2->host1 (rollback direction)
pgreplkit skip             # ALTER SUBSCRIPTION ... SKIP (confirmation required)
pgreplkit teardown         # remove created artifacts (confirmation required)
pgreplkit guide            # generate a manual runbook (SQL + AWS CLI), executes nothing
```

Global options (illustrative): `--config`, `--source`, `--target`, `--mode`
(`existing`|`provision`), `--databases`,
`--include-schemas`, `--exclude-schemas`, `--include-tables`, `--exclude-tables`,
`--slots`, `--n`, `--slot-map`, `--max-slots`, `--spread-partitions`, `--init-sync`,
`--concurrency`, `--statement-timeout`,
`--lock-timeout`, `--json`, `--dry-run`, `--generate-only`, `--yes`, `--verbose`.

## 7. Suggested Technology Choices (non-binding)

- **DB driver:** `psycopg` (v3) — mature; supports catalog inspection, replication
  management, and timeouts.
- **CLI framework:** `typer` or `click` — clean command/option modeling.
- **Output/formatting:** `rich` — readable tables and status displays.
- **Config:** `pydantic` (v2) for validated config models; YAML/TOML file support.
- **AWS integration:** `boto3` for reading RDS/Aurora metadata, parameter-group state,
  and (opt-in) driving snapshot restores / fast clones; AWS CLI snippets for guide
  mode.
- **Testing:** `pytest` with `testcontainers` (or a docker-compose Postgres pair) for
  logic; live RDS/Aurora clusters (profile `workshop`, region `us-east-1`) for
  managed-engine paths (LSN functions, parameter groups, roles, multi-DB clusters).

## 8. RDS / Aurora Notes (primary environment)

- **Enabling logical WAL:** on RDS/Aurora you do **not** set `wal_level` directly; set
  `rds.logical_replication = 1` in the parameter group. It is **static** and requires
  a reboot. Preflight/guide must key off this parameter and detect a pending reboot.
- **Roles:** there is no OS superuser. Publication/subscription management requires
  `rds_replication` / `rds_superuser` (or `pg_create_subscription` on PG16+); role
  recreation must use the managed API, not raw `pg_dumpall` of superuser roles.
- **Tablespaces:** user-defined tablespaces are restricted/unsupported on RDS/Aurora;
  map objects to the default tablespace on the target (FR-34).
- **System databases:** `rdsadmin` is managed by AWS and MUST be skipped (FR-5).
- **Consistent seed LSN for physical seeds — same-engine only:**
  - **RDS → RDS:** LSN continuity preserved. On the restored target:
    `CREATE EXTENSION rds_tools; SELECT rds_tools.logical_seed_lsn();`.
  - **Aurora → Aurora:** LSN continuity preserved. On the clone/restore:
    `SELECT aurora_volume_logical_start_lsn();`.
  - **RDS → Aurora / Aurora → RDS:** out of scope (see §10). RDS→Aurora would suffer
    LSN divergence (Aurora regenerates its LSN sequence, so the Aurora function does
    not map to the RDS snapshot point); such cross-engine cases should use `copy` or
    AWS DMS instead.
  Start the subscription with `copy_data = false`, advance the replication origin
  (`pg_replication_origin_advance('pg_<subid>', <seed_lsn>)`), then `ENABLE` so
  post-seed changes replay exactly once (see FR-50).
- **Provisioning orchestration:** in `provision` mode the tool creates the target via
  the **AWS SDK (boto3)** from Python (assuming a properly configured AWS
  environment/credentials), not by shelling out to the AWS CLI; guide/`--generate-only`
  mode instead emits the equivalent AWS CLI commands for manual execution.
- **Aurora fast clone:** copy-on-write, shares the source timeline; use the
  clone-then-promote flow and capture the seed LSN on the clone.
- **WAL retention during seeding:** the slot created before the snapshot retains WAL
  on the source until the subscription is enabled; monitor CloudWatch
  `OldestReplicationSlotLag`, `ReplicationSlotDiskUsage`, and `FreeStorageSpace` to
  avoid a source disk-full outage.
- **PG16+ read-replica source:** RDS PG16+ can serve logical replication from a read
  replica, offloading decode CPU/memory from the primary.

## 9. Key Risks & Assumptions

- **Risk (highest): WAL/slot bloat outage.** An inactive or lagging slot retains WAL
  on blue and can fill its disk; more slots (balanced/per-schema/manual) means more
  slots to
  keep active. Mitigated by FR-29 (preflight) and FR-55 (monitoring `wal_status`,
  retained bytes, inactive/lost alerts).
- **Risk: cross-slot inconsistency.** Multi-slot strategies tear cross-table
  transactions during streaming and can transiently violate cross-slot FKs. Mitigated
  by FR-13 (warn + keep FK tables together), FR-51 (`session_replication_role`), and
  convergence at the FR-65 drain step.
- **Risk: silent target-schema mismatch.** `copy`/pre-seeded strategies fail
  immediately against an empty/mismatched green. Mitigated by FR-18/FR-19.
- **Risk: missing global objects.** Missing roles/tablespaces break restore/apply.
  Mitigated by FR-32–FR-35.
- **Risk: seed-LSN ordering.** Physical seeds must resume from the exact consistent
  LSN or rows are lost/duplicated. Mitigated by FR-48–FR-50 and the RDS/Aurora LSN
  functions in §8.
- **Risk: "lag = 0" ≠ correct.** A caught-up subscription can still have a silently
  failed table. Mitigated by FR-61/FR-62 (validate) and the composite gate FR-64.
- **Risk: sequence collision at cutover.** `sync-sequences` after writes stop on blue
  is enforced by FR-65 ordering.
- **Risk: replication loop on reverse.** Establishing reverse while forward is active
  loops. Mitigated by FR-72 (tear down forward first, or `origin = none` on PG16+).
- **Risk: stuck apply worker.** Target triggers/FKs firing during apply can block the
  worker. Mitigated by FR-51, FR-58–FR-60.
- **Risk: version feature mismatch.** Mitigated by FR-20/FR-21 feature gating.
- **Risk: credential in `pg_subscription`.** Mitigated by FR-45 (least-privilege user,
  documented handling).
- **Assumption:** The user can grant the required privileges/roles on both clusters
  and, for AWS provisioning steps, holds the necessary AWS permissions.
- **Assumption:** For `snapshot-restore`/`aurora-fast-clone`, source and target are
  RDS/Aurora PostgreSQL.

## 10. Out of Scope (v1)

- **Cross-engine pairs (RDS↔Aurora).** Only same-engine setups are supported
  (RDS→RDS, Aurora→Aurora). Cross-engine physical seeding is specifically excluded
  because RDS-snapshot→Aurora restore causes LSN divergence — Aurora regenerates its
  own LSN sequence, so `aurora_volume_logical_start_lsn()` on the Aurora target does
  not map to the RDS snapshot point, and resuming would require restoring a temporary
  RDS instance to extract `rds_tools.logical_seed_lsn()`. That complexity is out of
  scope; cross-engine migrations should use the `copy` strategy or AWS DMS.
- Authoring or migrating schema/DDL (verification only).
- Steady-state bidirectional / active-active replication (reverse is a one-direction
  rollback path).
- Traffic/connection-string switchover automation.
- A graphical or web UI (CLI only for v1).
- Conflict resolution beyond what native logical replication provides.
- Full major-version upgrade orchestration beyond enabling cross-version replication.
