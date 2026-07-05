# pgreplkit — Design

This document describes the architecture and implementation design for **pgreplkit**,
the tool specified in [REQUIREMENTS.md](./REQUIREMENTS.md). Requirement references
(FR-n / NFR-n) point back to that document.

## 1. Design Goals & Principles

- **Read-heavy, write-careful.** Discovery/preflight/validate are strictly read-only
  (FR-31). Every state-changing action flows through a single execution layer that
  supports execute / dry-run (FR-40) / generate-only (§4.15).
- **Plan then apply.** Each phase produces an explicit, serializable *plan* that can be
  inspected, printed as SQL, or turned into a guide before anything runs. The plan is
  the shared artifact between execute mode and guide mode (FR-75–FR-80).
- **Manifest is the source of truth.** All created objects are recorded in a local
  manifest (FR-74); `status`, `resume`, `reverse`, and `teardown` operate from it, not
  from naming heuristics alone.
- **Engine-aware, not engine-coupled.** A small capability layer abstracts
  self-managed PostgreSQL vs RDS vs Aurora so phase logic stays clean while honoring
  managed-engine constraints (FR-24, FR-28, FR-33, FR-34, FR-49, §8).
- **Fail closed on safety.** Blocking preflight failures stop setup unless explicitly
  overridden (FR-38); destructive actions require confirmation (NFR-3); the cutover
  state machine refuses to advance on nonzero lag or failed validation (FR-65).

## 2. High-Level Architecture

```
                         ┌──────────────────────────────┐
                         │            CLI (typer)         │  cli.py
                         │  discover plan preflight ...   │
                         └───────────────┬────────────────┘
                                         │ builds Context (config, mode, engine caps)
                         ┌───────────────▼────────────────┐
                         │            Phases               │  phases/*
                         │  each: gather facts → build     │
                         │  Plan → (execute|dryrun|guide)  │
                         └───┬──────────┬──────────┬───────┘
                             │          │          │
                 ┌───────────▼──┐  ┌────▼─────┐  ┌─▼──────────┐
                 │   Catalog    │  │ SlotPlan │  │  Manifest  │  core/*
                 │ (RO queries) │  │ (packing)│  │  (state)   │
                 └───────┬──────┘  └──────────┘  └────────────┘
                         │
        ┌────────────────▼─────────────────┐      ┌──────────────────┐
        │  Connection / Executor           │◄─────┤  Engine caps /    │
        │  (psycopg3, timeouts, dry-run)   │      │  version gating   │  checks/, aws/
        └────────────────┬─────────────────┘      └──────────────────┘
                         │
              ┌──────────▼───────────┐   ┌────────────────────────┐
              │  source / target PG  │   │  boto3 RDS/Aurora APIs  │
              └──────────────────────┘   └────────────────────────┘
                         │
              ┌──────────▼───────────┐
              │  Report (rich/JSON)  │  report/*
              └──────────────────────┘
```

Data flows one direction within a phase: **facts (read) → plan (pure) → effects
(execute / render)**. The "plan" step is pure and side-effect free, which makes it
unit-testable and reusable by guide mode.

## 3. Package Layout

```
pgreplkit/
├── pyproject.toml               # packaging, entry point `pgreplkit` (NFR-1)
├── README.md
├── REQUIREMENTS.md
├── DESIGN.md
└── src/pgreplkit/
    ├── __init__.py
    ├── __main__.py              # python -m pgreplkit
    ├── cli.py                   # typer app; maps commands → phases
    ├── context.py               # Context: config, ExecutionMode, engine caps, logger
    ├── errors.py                # typed exceptions + exit-code mapping (NFR-6)
    ├── logging.py               # verbosity, structured/JSON logs (NFR-5)
    ├── config/
    │   ├── models.py            # pydantic v2 models (source/target, scope, slots…)
    │   ├── loader.py            # YAML/TOML + env + secrets (FR-1..3)
    │   └── slotmap.py           # manual slot-map schema + parser (FR-12)
    ├── core/
    │   ├── connection.py        # connect, apply statement/lock timeouts (FR-39, NFR-10)
    │   ├── catalog.py           # all read-only SQL fact-gathering (FR-31)
    │   ├── engine.py            # EngineKind detection + capability facts (§8)
    │   ├── topology.py          # DB/schema/table discovery + skip rules (FR-4..9)
    │   ├── slotplan.py          # strategies, FK graph, weighted bin-packing (FR-10..13)
    │   ├── naming.py            # object naming convention (FR-43)
    │   ├── sqlgen.py            # emit CREATE/ALTER/DROP statements (pure)
    │   ├── executor.py          # execute | dry-run | generate-only dispatch
    │   └── manifest.py          # manifest read/write/merge (FR-74)
    ├── checks/
    │   └── version.py           # feature availability by PG version (FR-20, FR-21)
    ├── aws/
    │   ├── rds.py               # boto3: describe/create RDS/Aurora, snapshots, clones, params, seed LSN (§8)
    │   └── permissions.py       # provision-mode IAM/quota/network prereq checks (FR-52a)
    ├── phases/
    │   ├── discover.py          # FR-4..9
    │   ├── preflight.py         # FR-14..31, FR-32..35 (globals check hook)
    │   ├── globals_.py          # roles & tablespaces (FR-32..35)
    │   ├── plan.py              # emit balanced layout as editable YAML (FR-12)
    │   ├── setup.py             # publications/subscriptions/slots (FR-36..45)
    │   ├── initial_sync.py      # copy | snapshot-restore | aurora-fast-clone (FR-46..53)
    │   ├── monitor.py           # status/watch, slot & WAL health (FR-54..57)
    │   ├── apply_ops.py         # disable_on_error, skip (FR-58..60)
    │   ├── validate.py          # row counts / checksums (FR-61, FR-62)
    │   ├── cutover.py           # ordered state machine (FR-63..65)
    │   ├── reverse.py           # direction swap + loop avoidance (FR-70..73)
    │   ├── teardown.py          # FR-66..69
    │   └── guide.py             # runbook renderer (FR-75..80)
    └── report/
        ├── render.py            # rich tables + JSON serialization (NFR-5)
        └── models.py            # report/result dataclasses, exit-code carriers
└── tests/
    ├── unit/                    # pure logic: slotplan, sqlgen, version, config
    └── integration/            # docker-compose PG pair + live RDS/Aurora (profile workshop)
```

## 4. Core Domain Model

Pydantic models (config) and frozen dataclasses (runtime plans). Simplified:

```python
class EngineKind(StrEnum): VANILLA="vanilla"; RDS="rds"; AURORA="aurora"

class Endpoint(BaseModel):          # FR-1..3
    host: str; port: int = 5432
    user: str; password: SecretStr | None = None   # never logged (FR-2)
    dbname: str | None = None       # maintenance db for discovery
    secret_ref: str | None = None   # e.g. AWS Secrets Manager ARN

class Scope(BaseModel):             # FR-8, FR-9
    databases: list[str] | None
    include_schemas: list[str] = ["*"]
    exclude_schemas: list[str] = []
    include_tables: list[str] = ["*"]
    exclude_tables: list[str] = []
    skip_system_dbs: bool = True    # template0/1, rdsadmin, datallowconn=false (FR-5)
    skip_empty_dbs: bool = True     # FR-6

class SlotStrategy(StrEnum): SINGLE="single"; PER_SCHEMA="per-schema"; \
                             BALANCED="balanced"; MANUAL="manual"

class SlotConfig(BaseModel):        # FR-10..13
    strategy: SlotStrategy = SlotStrategy.BALANCED   # default (FR-10)
    n: int = 4                      # balanced slot count
    max_slots: int = 8              # decode-cost cap (FR-13)
    spread_partitions: bool = False # FR-11 modifier
    slot_map: Path | None = None    # required for MANUAL (FR-12)
    weight_window: timedelta | None = None  # stat sampling window

class InitSync(StrEnum): COPY="copy"; SNAPSHOT_RESTORE="snapshot-restore"; \
                         AURORA_FAST_CLONE="aurora-fast-clone"; NONE="none"

class ProvisionMode(StrEnum): EXISTING="existing"; PROVISION="provision"  # FR-52

class ExecutionMode(StrEnum): EXECUTE="execute"; DRY_RUN="dry-run"; GENERATE_ONLY="generate-only"
```

Runtime plan objects (pure, produced by phases, consumed by executor/guide):

```python
@dataclass(frozen=True)
class TableRef: schema: str; name: str
                # fully-qualified, quoted on render

@dataclass(frozen=True)
class SlotSpec:                     # one publication+subscription+slot
    db: str; index: int             # (db, index) is the identity used in naming (FR-43)
    tables: tuple[TableRef, ...]
    publication: str; subscription: str; slot_name: str
    publish_ops: frozenset[str]     # insert/update/delete/truncate (FR-37)
    via_partition_root: bool

@dataclass(frozen=True)
class DatabasePlan: db: str; slots: tuple[SlotSpec, ...]; strategy: SlotStrategy
@dataclass(frozen=True)
class ClusterPlan: databases: tuple[DatabasePlan, ...]; direction: Direction
```

## 5. Execution Model (execute / dry-run / generate-only)

A single `Executor` mediates every state change so the three modes share one code
path (DRY the safety logic):

```python
class Executor:
    def __init__(self, conn, mode: ExecutionMode, sink: SqlSink): ...
    def run(self, stmt: Sql) -> Result:
        if self.mode is EXECUTE:        return self._exec(stmt)   # real
        if self.mode is DRY_RUN:        return self._print(stmt)  # show, no-op
        if self.mode is GENERATE_ONLY:  return self._record(stmt) # into runbook
```

- `sqlgen.py` builds statements as data (`Sql(text, params, phase, note)`), never as
  interpolated strings, so parameters are bound safely and the same object can be
  executed, printed, or written into a `.sql`/Markdown runbook.
- **Timeouts** (FR-39, NFR-10): before long/locking statements the executor issues
  `SET LOCAL statement_timeout` / `lock_timeout` on the session.
- **Guide mode** (§4.15) is simply `GENERATE_ONLY` with an additional read-only
  connection allowed for fact-gathering (FR-76); if no connection is permitted, phases
  fall back to templated output from config.

## 6. Phase Designs

### 6.1 Discover (FR-4–FR-9)
`catalog.list_databases()` reads `pg_database` and applies skip rules:
`datistemplate`, `datallowconn=false`, and the name denylist
`{template0, template1, rdsadmin}` (FR-5). For each remaining DB, connect and count
user tables (`pg_class` reltype filtered to `pg_namespace` excluding
`pg_catalog/information_schema/pg_toast`); empty DBs are skipped (FR-6). If the
`postgres` database has user tables, attach a `NOT_BEST_PRACTICE` warning but keep it
in scope (FR-7). Output: a `TopologyReport` (in-scope vs skipped with reasons),
renderable as table or JSON (FR-30).

### 6.2 Slot Planning (FR-10–FR-13) — `core/slotplan.py`
Pure functions, fully unit-testable, no DB access (facts passed in).

- **Weights:** `TableWeight = n_tup_ins + n_tup_upd + n_tup_del` from
  `pg_stat_all_tables`, read **point-in-time by default**, optionally scaled by
  `avg_row_width` from `pg_stats` (apply cost ≈ bytes). A two-read sampling window is
  a non-default refinement.
- **FK-affinity groups:** build an undirected graph where an edge connects two tables
  sharing a foreign key (`pg_constraint contype='f'`); connected components via
  union-find form indivisible groups (FR-11).
- **Packing (`balanced`):** greedy longest-processing-time — sort groups by total
  weight desc, assign each to the least-loaded of `N` bins:

  ```python
  def pack(groups, n):
      bins = [Bin() for _ in range(n)]
      for g in sorted(groups, key=lambda g: -g.weight):
          min(bins, key=lambda b: b.weight).add(g)
      return bins
  ```
- **Presets over the engine:** `single` → n=1; `per-schema` → force group boundary =
  schema then one bin per schema; `manual` → parse slot-map, validate partition
  coverage/precedence/ambiguity/cross-slot FK (FR-12).
- **Partition spreading:** when enabled, a partitioned parent is expanded to its
  partitions (`pg_inherits`) as separate packable units with
  `via_partition_root=false` (FR-11).
- **Caps:** total slots checked against `max_slots` and, later, against server headroom
  (FR-13, FR-23, FR-26); emit `DecodeCostWarning` as N grows.

### 6.3 Preflight (FR-14–FR-31)
Aggregates independent read-only checks, each returning
`CheckResult{level, code, message, remediation}`; `level ∈ {ok, warn, block}`.

| Check | Source | Requirement |
|---|---|---|
| Replica identity (+ `FULL` cost warning) | `pg_class.relreplident`, PK/unique index | FR-14–16 |
| Non-replicable relation kinds | `pg_class.relkind`/`relpersistence` (block views/matviews/foreign; warn unlogged/temp; pre-PG18 generated cols) | FR-17a |
| Unreplicated objects (seq/LO) | `pg_sequence`, `pg_largeobject_metadata` | FR-17 |
| Target schema exists & matches | compare `information_schema.columns` src↔tgt | FR-18, FR-19 |
| Version & feature gating | `SHOW server_version_num` → `checks/version.py` | FR-20, FR-21 |
| Encoding/collation parity | `pg_database` encoding, `datcollate/datctype` | FR-22 |
| Source prereqs | `wal_level`/`rds.logical_replication`, slots/senders | FR-23, FR-24 |
| Source privileges/roles | `pg_roles`, `has_table_privilege` | FR-25 |
| Target prereqs | worker/slot GUCs | FR-26 |
| Target→source reachability | attempt subscriber-style connect back | FR-27, FR-28 |
| WAL retention (unbounded vs bounded) | `max_slot_wal_keep_size` value + expected retention | FR-29 |
| AWS permissions & parameters (both modes; provision mode adds IAM/quotas/networking) | boto3 dry-run / `sts`/`iam` simulate, quota + subnet/SG/KMS reads | FR-52a |

`preflight` and `discover` open **read-only** sessions
(`default_transaction_read_only=on`) as defense-in-depth (FR-31). Runs before `setup`
and blocks on any `block`-level result. Overrides distinguish **correctness** blocks
(missing replica identity → louder, separate confirmation, recorded in manifest) from
**capacity** blocks (FR-38).

### 6.4 Global Objects (FR-32–FR-35) — `phases/globals_.py`
Detect roles that own/are-granted on in-scope objects (`pg_class.relowner`,
`pg_default_acl`, `information_schema.role_table_grants`) and compare to target
`pg_roles` (FR-32). Detect non-default tablespaces via `pg_class.reltablespace`
(FR-34). Reproduce roles/grants (equivalent to `pg_dumpall --globals-only`, filtered)
and tablespaces; on RDS/Aurora, route role creation through managed SQL and **map
objects to the default tablespace** since user tablespaces are unsupported (FR-33,
FR-34, §8). Unmet items are `block`-level for the affected objects (FR-35).

**Managed-env password handling (FR-33):** source role passwords cannot be recovered
(`pg_dumpall` cannot dump unknown passwords in RDS/Aurora). When a missing role must
be created and its password is unavailable, `globals_.py` generates a strong random
password (`secrets.token_urlsafe`), creates the role with it, and writes
`{role: password}` to a protected credentials sink (a `0600` file or a configured
secret store) referenced by role name — never to normal logs (ties to FR-2). The
result report lists which roles got generated passwords so the DBA can rotate them.

### 6.5 Setup (FR-36–FR-45)
For each `DatabasePlan`/`SlotSpec`, generate and execute via the Executor:
`CREATE PUBLICATION … FOR TABLE … WITH (publish=…, publish_via_partition_root=…)`
(FR-37) on source, then `CREATE SUBSCRIPTION … CONNECTION … PUBLICATION …
WITH (copy_data=<per initial-sync>, create_slot=true, slot_name=…, disable_on_error=…)`
(FR-58) on target. **Idempotency with conflict detection (FR-41):** before creating,
check catalog + manifest; if an object exists, compare it against the computed plan
(publication table set + `publish` ops; subscription slot/options) and **adopt only on
exact match** — on divergence raise `ManifestConflict`/`PlanConflict` and refuse rather
than silently adopting a mismatched object. Naming (FR-43): `naming.py` yields
deterministic names `pgrk_<run>_<db>_<idx>` for slot/pub/sub. On partial failure the manifest records
what succeeded to enable resume (FR-42, FR-74). Connection-secret handling: prefer a
dedicated least-privilege replication role and document `pg_subscription` exposure
(FR-45). `refresh` performs `ALTER PUBLICATION … ADD TABLE` + `ALTER SUBSCRIPTION …
REFRESH PUBLICATION` for newly appeared tables (FR-44).

### 6.6 Initial Sync (FR-46–FR-53)
Strategy pattern:

- **copy:** `copy_data=true`; poll `pg_subscription_rel.srsubstate` until all `r`
  (ready); expose `max_sync_workers_per_subscription` (FR-47).
- **snapshot-restore / aurora-fast-clone:** `copy_data=false`. **Provisioning mode
  (FR-52):** in `existing` mode both clusters already exist and the tool only wires up
  replication; in `provision` mode `aws/rds.py` creates the target **matching the
  source engine** (RDS→RDS or Aurora→Aurora) via **boto3** (assuming a configured AWS
  environment) — never by shelling out to the AWS CLI — while guide/`--generate-only`
  mode emits the equivalent AWS CLI. Provisioning is confirmation-gated (billable) and
  preceded by the `provision`-mode prerequisite check (FR-52a: IAM permissions,
  quotas, subnet group / security groups / KMS). Cross-engine targets are never
  provisioned (§10).

  **Seed-LSN capture — same-engine only (FR-49):**

  | source → target | seed-LSN method |
  |---|---|
  | RDS → RDS | `rds_tools.logical_seed_lsn()` on restored target (LSN continuous) |
  | Aurora → Aurora | `aurora_volume_logical_start_lsn()` on clone (LSN continuous) |
  | RDS ↔ Aurora (cross-engine) | **out of scope** — LSN diverges; use `copy` or DMS |

  **Exactly-once resume sequence (FR-50)** — verified against AWS guidance:
  ```sql
  -- (1) BEFORE snapshot, on source:
  SELECT pg_create_logical_replication_slot('pgrk_<...>', 'pgoutput');
  -- (2) snapshot/clone; provision target (same engine); capture seed LSN on target
  -- (3) on source:
  CREATE PUBLICATION pgrk_<...> FOR TABLE ...;
  -- (4) on target:
  CREATE SUBSCRIPTION pgrk_<...> CONNECTION '...' PUBLICATION pgrk_<...>
    WITH (copy_data=false, create_slot=false, enabled=false, connect=true,
          slot_name='pgrk_<...>');
  -- (5) advance origin to the captured seed LSN:
  SELECT 'pg_'||oid FROM pg_subscription WHERE subname='pgrk_<...>';  -- -> pg_<oid>
  SELECT pg_replication_origin_advance('pg_<oid>', '<seed_lsn>');
  -- (6) begin streaming exactly at the seed:
  ALTER SUBSCRIPTION pgrk_<...> ENABLE;
  ```
  Omitting step (5) makes the subscriber replay from the slot's pre-seed
  `confirmed_flush_lsn` → **duplicate rows** (PK conflict) or, with a wrong LSN,
  **missing rows**. `initial_sync.py` implements this as an explicit ordered
  transaction script; the origin-name format `pg_<oid>` is derived from
  `pg_subscription`, not hard-coded.

  **Exactly-once boundary test (integration, vanilla PG — no RDS needed):** create a
  slot, insert rows, `pg_export_snapshot()`/`COPY` to seed a target, capture the
  slot's LSN, run the resume sequence with `pg_replication_origin_advance`, then verify
  post-seed inserts/updates/deletes land exactly once (row counts + checksums match,
  no duplicate-key errors). A negative test omits the origin advance and asserts the
  duplicate/again behavior, locking in why step (5) is required.
- **none/pre-seeded:** wire up only.

`session_replication_role=replica` guidance applied on the apply side to avoid
trigger/FK stalls (FR-51). Per-table sync state reported (FR-53).

### 6.7 Monitoring (FR-54–FR-57) — `phases/monitor.py`
`status` joins, per db/slot: on the **source** `pg_replication_slots`
(`active`, `wal_status`, `safe_wal_size`, retained bytes) — this is also the
**authoritative lag source**: `pg_wal_lsn_diff(pg_current_wal_lsn(),
confirmed_flush_lsn)` (bytes behind), optionally cross-checked with
`pg_stat_replication.replay_lag`; plus `pg_subscription_rel` (initial-sync progress)
and `pg_stat_subscription` (receiver timestamps, *supplementary only*). Byte-lag off
`confirmed_flush_lsn` is what the `ready` gate uses (FR-54, FR-64), because it reflects
committed WAL not yet confirmed applied — receiver timestamps can read "current" while
committed changes remain unapplied. Emits `warn` on inactive slot / retained-WAL over
threshold / unbounded `max_slot_wal_keep_size`, `error` on `wal_status='lost'`
(FR-29, FR-55). `watch` re-polls until caught-up or Ctrl-C (FR-57). Apply-worker errors
surfaced from `pg_stat_subscription`/stats (FR-56). To catch **DDL drift during live
replication** before it wedges a worker, `status`/`watch` also re-compare source/target
column definitions for in-scope tables and warn on divergence proactively (FR-56).

### 6.8 Apply Ops (FR-58–FR-60)
`disable_on_error` set at creation where supported (FR-58). `skip` issues
`ALTER SUBSCRIPTION … SKIP (lsn=…)` behind explicit confirmation because it drops data
(FR-59). Reporting identifies the affected slot/subscription and warns that a stuck
worker blocks that slot's stream (FR-60).

### 6.9 Validate (FR-61, FR-61a, FR-62)
**Object & global-object counts (always, any depth):** compare in-scope object counts
(tables, sequences, indexes, **and `pg_largeobject_metadata` where LOs are used**) and
the set of roles/users between source and target, reporting anything present on source
but missing on target (FR-61a, FR-17, ties to FR-32/FR-33).

**Row/data comparison (selectable depth, default `sampled`):**
- `none` — object/count checks only.
- `sampled` — per-table `COUNT(*)` plus a sampled checksum
  (`md5(array_agg(...))` over a sampled key range, or `TABLESAMPLE`).
- `full` — full-table checksum, opt-in **per table** (expensive).

Returns per-table pass/fail and object discrepancies; nonzero process exit on any
failed table or object-count check so it can gate cutover (FR-62, NFR-6).

### 6.10 Cutover (FR-63–FR-65) — state machine
```
        ┌────────┐  writes stopped?  ┌────────┐  lag==0 (all slots)?  ┌────────┐
QUIESCE─┤ QUIESCE├──────────────────►│ DRAIN  ├──────────────────────►│ SEQSYNC│
        └────────┘   (confirm)       └───┬────┘   refuse if lag>0      └───┬────┘
                                         │ timeout→ABORT                   │
                     ┌────────┐          │                     ┌──────────▼─────┐
        READY ◄──────┤ VALIDATE│◄────────┘                     │ sync-sequences │
      (signal only)  └────────┘  refuse READY if validation    └────────────────┘
                                 fails (FR-62)
```
`sync-sequences` runs **after** writes stop (else green sequences collide) — enforced
by state order (FR-63, FR-65). `ready` is a composite gate: initial-sync complete AND
no apply errors AND slot active/not-lost AND lag<threshold, across all db/slots, with
nonzero exit on fail (FR-64).

### 6.11 Reverse (FR-70–FR-73)
Reads the manifest to identify the existing forward artifacts (FR-73). Reverse is a
swap **from an in-sync state**: the operator stops the forward direction at zero lag
(both clusters equivalent), so the reverse subscription is created with initial-sync
`none` (`copy_data=false`) and no data re-copy (FR-70). Runs preflight in the reverse
direction — former target must now satisfy source requirements, former source needs
target/schema/globals checks — and verifies forward was stopped at zero lag so no
post-swap writes are missed (FR-71). **Loop avoidance (FR-72):** default requires
forward direction torn down/inactive before creating reverse; bidirectional is refused
unless the user opts in on PG16+ where subscriptions are created `WITH (origin = none)`.
New direction recorded in manifest.

### 6.12 Teardown (FR-66–FR-69)
From the manifest, drop subscriptions (which drops remote slots when `slot_name` owned),
then publications; verify no orphan slot remains on source (`pg_replication_slots`),
offering `pg_drop_replication_slot` for orphans since they retain WAL (FR-69).
Confirmation required; `--dry-run` supported (FR-67). Objects not in the manifest and
not matching the naming convention are never dropped without explicit opt-in (FR-68).

### 6.13 Guide (FR-75–FR-80)
Guide mode runs each phase's *plan* step (read-only allowed) and feeds the resulting
`Sql` objects and out-of-band steps into a renderer that emits a Markdown runbook and
`.sql` scripts (FR-78). It includes scope/skip decisions, slot layout, prereqs
(including RDS `rds.logical_replication` + reboot), globals, `pg_dump --schema-only`,
per-slot pub/sub SQL, the seed-LSN capture, validation queries, ordered cutover, and
teardown, annotating every out-of-SQL / ordering-dependent step (FR-77, FR-80). Uses
the same naming convention so tool `status`/`teardown` recognize manually-created
artifacts (FR-79).

## 7. Manifest (FR-74)

`~/.pgreplkit/<project>.json` (path configurable).

**Concurrency-safe writes (fixes a last-writer-wins data-loss bug):** setup runs
per-slot work in parallel (§9), so worker threads must **not** each rewrite the single
manifest file — concurrent temp+rename would lose updates. The design uses a single
**manifest-owner**: one dedicated thread (or the main thread) owns the manifest and is
the only writer; worker threads publish completed-slot records onto a thread-safe queue
(or via a `threading.Lock`-guarded API), and the owner applies them and does the atomic
temp-write+`os.replace` after each merge (NFR-8). Equivalently, workers may write
**per-slot fragment files** that the owner merges into the manifest at barriers. Either
way there is exactly one writer of the manifest file at a time.

```json
{
  "schema_version": 1,
  "project": "prod-bluegreen",
  "run_id": "20260704T190000Z-ab12",
  "direction": {"source": "host1", "target": "host2"},
  "created_at": "...", "updated_at": "...",
  "databases": {
    "appdb": {
      "strategy": "balanced",
      "slots": [
        {"index": 0, "slot_name": "pgrk_ab12_appdb_0",
         "publication": "pgrk_ab12_appdb_0", "subscription": "pgrk_ab12_appdb_0",
         "tables": ["public.orders", "public.order_items"],
         "publish": ["insert","update","delete"], "via_partition_root": true,
         "init_sync": "aurora-fast-clone", "seed_lsn": "0/402E2F0",
         "state": "streaming"}
      ]
    }
  }
}
```

## 8. Engine Capability Layer (§8 of requirements)

`core/engine.py` detects `EngineKind` (Aurora via `aurora_version()`, RDS via
`rds_tools`/`rds.*` GUCs, else vanilla) and exposes a `Capabilities` object consumed
by phases:

| Capability | vanilla | RDS | Aurora |
|---|---|---|---|
| enable logical WAL | `wal_level=logical` | `rds.logical_replication=1` + reboot | same (cluster PG) |
| create sub role | superuser | `rds_replication`/`rds_superuser`/`pg_create_subscription` | same |
| user tablespaces | yes | no (map to default) | no |
| seed LSN fn | n/a | `rds_tools.logical_seed_lsn()` | `aurora_volume_logical_start_lsn()` |
| provisioning (provision mode) | n/a | RDS instance via boto3 | Aurora cluster via boto3 |
| snapshot/clone | n/a | RDS restore (boto3) | fast clone (boto3) |

`checks/version.py` maps `server_version_num` to a feature set
(`truncate≥11`, `streaming≥14`, `two_phase≥14/15`, `row_filter/column_list/schema_pub≥15`,
`parallel_apply/origin_none≥16`) for FR-20/FR-21 gating.

## 9. Concurrency, Errors, Exit Codes

- **Concurrency (NFR-4):** per-database/per-slot work runs via a bounded
  `ThreadPoolExecutor` (psycopg3 is largely I/O-bound; threads avoid an async
  rewrite). `--concurrency` caps parallelism; reporting is collected deterministically
  and rendered in stable order. **Connection ownership:** psycopg3 connections are not
  safe for concurrent use by multiple threads, so each worker thread owns its own
  connection(s) from a per-thread pool — connections are never shared across threads.
  Manifest updates are funneled to the single manifest-owner (§7), not written by
  workers directly.
- **Errors (errors.py):** typed exceptions (`PreflightBlocked`, `EngineUnsupported`,
  `SlotCapExceeded`, `ManifestConflict`, …) each map to a stable **exit code** (NFR-6)
  so CI gates (`ready`, `validate`, `preflight`) are scriptable.
- **Logging (NFR-5):** `--verbose`/`--json`; secrets are redacted at the formatter
  layer (FR-2).

## 10. Testing Strategy

- **Unit (fast, no DB):** slot planning (FK grouping + bin-packing determinism, cap
  enforcement), sqlgen (statement + param correctness, quoting), version gating,
  config/slot-map parsing/validation, manifest merge/atomicity, cutover state-machine
  transitions and refusals.
- **Integration (docker-compose, two PG containers):** discover→preflight→setup→copy
  →status→validate→cutover→teardown happy path; replica-identity blocking; refresh
  after adding a table; reverse with loop-avoidance.
- **Managed-engine (live, profile `workshop`, us-east-1):** `rds.logical_replication`
  detection + reboot handling, role/tablespace constraints, and the seed-LSN paths
  (`aurora_volume_logical_start_lsn()` / `rds_tools.logical_seed_lsn()`), gated behind
  an opt-in marker so they don't run in normal CI.
- Tooling: `pytest`, `testcontainers` (or compose), coverage on `core/` and `checks/`.

## 11. Resolved Design Decisions

- **Balanced packing input:** use **point-in-time** `pg_stat_all_tables` as the
  default weight source (optionally scaled by `avg_row_width`); a two-read sampling
  window is an optional non-default refinement. *(§6.2, FR-11)*
- **Reverse initial sync:** reverse is performed **from an in-sync state** — the
  operator stops/removes the forward direction at zero lag, so both clusters are
  equivalent and the reverse subscription uses initial-sync **`none`** (no re-copy).
  There is no data-stability concern because the stop-forward / start-reverse
  transition is controlled from a consistent point. *(§6.11, FR-70/FR-71)*
- **Sequence handling:** sequences are synchronized **only at cutover** (after writes
  stop on the source); no periodic sequence-drift reporting in `status`. *(§6.10,
  FR-63)*
- **Validation depth:** an **optional, user-selectable** per-row depth — `none`,
  `sampled` (default: row-count + sampled checksum), or `full` (opt-in per table).
  **Regardless of depth**, `validate` always compares object counts (tables,
  sequences, indexes, …) and global objects (roles/users). *(§6.9, FR-61/FR-61a/FR-62)*
- **Managed-env role passwords:** since source passwords can't be dumped in RDS/Aurora,
  missing roles are created with a strong random password that is recorded to a
  protected credentials sink for DBA reference (never in normal logs) and reported.
  *(§6.4, FR-33)*

