# pgreplkit

Automate and de-risk **PostgreSQL logical replication** for blue-green deployments and
low-downtime migrations — with first-class support for **Amazon RDS and Aurora
PostgreSQL**.

pgreplkit turns the fragile, manual process of standing up logical replication across
many databases into a validated, repeatable workflow:

```
discover → preflight → globals → (initial data sync) → setup → monitor
        → validate → cutover → (optional reverse for rollback)
```

It handles the parts that homegrown scripts get wrong: replica-identity checks, WAL/slot
bloat, seed-LSN ordering for physical seeds, unreplicated objects (sequences, large
objects, roles, tablespaces), cross-slot consistency, and version/feature gating.

## Interactive setup guide (no install required)

Prefer to set replication up **by hand**, or just want to understand each step? An
interactive, scenario-driven guide is published via GitHub Pages:

**➡ https://luodonghua.github.io/pgreplkit/**

Pick your source (self-managed, RDS, Aurora, Cloud SQL, AlloyDB, Azure Flexible Server),
target (RDS or Aurora), initial-sync method, and scope, and it generates a tailored,
copy-paste runbook — prerequisites, setup SQL, monitoring, cutover, troubleshooting, and
flow diagrams, with links to the official PostgreSQL/AWS/GCP/Azure docs. It also covers
the PG16 `origin = none` **bi-directional** setup. The guide is standalone (it does not
require pgreplkit); the source lives under [`gh-pages/`](gh-pages/).

## Features

- **Discovery & preflight** — enumerate databases/schemas/tables with skip rules;
  read-only checks for replica identity (including the caveat that a published table
  with no identity blocks UPDATE/DELETE on the *source*, and that `REPLICA IDENTITY FULL`
  fails to apply for column types with no default B-tree/hash operator class),
  non-replicable relation kinds, target schema and column parity (name/type/nullability/
  **generated status**), encoding/collation parity, source/target prerequisites, and
  WAL-retention safety.
- **Sizing & planning** — a read-only `sizing` report of per-table storage (table +
  index) and recent write activity (inserts/updates/deletes per second) across the
  replication scope, to inform copy-vs-physical-seed choice and lag expectations.
- **Slot allocation** — `single`, `per-schema`, `balanced` (FK-affinity grouping +
  weighted bin-packing into N slots, the default), or `manual` (explicit slot map with
  explicit-name > glob > catch-all precedence), with a publisher decode-cost cap. An
  opt-in **partition-spreading** modifier (`--spread-partitions`) distributes a hot
  partitioned table's leaf partitions across slots.
- **Initial sync strategies** — built-in `copy`; physical `snapshot-restore` (RDS) and
  `aurora-fast-clone` with exactly-once **seed-LSN resume**; or `none` (pre-seeded).
- **Provisioning** — `existing` mode (bring your own green) or `provision` mode (create
  a same-engine green via boto3; RDS→RDS or Aurora→Aurora only).
- **Monitoring** — per-slot subscription state, initial-sync progress, and slot/WAL
  health; lag measured off the source slot's `confirmed_flush_lsn`.
- **Validation & cutover** — object/role/row comparison; an ordered cutover state
  machine (quiesce → drain → sequences → validate → signal); a composite `ready` gate.
- **Rollback** — `reverse` flips an in-sync setup into the opposite direction.
- **Generate-only mode** — `guide` emits a full SQL + AWS CLI runbook and executes
  nothing (for change-controlled environments), with secrets redacted.

## Install

```bash
pip install -e .            # or: uv pip install -e ".[dev]"
pgreplkit --help
```

Requires Python 3.10+. Key dependencies: `psycopg[binary]`, `typer`, `pydantic`,
`rich`, `boto3`, `PyYAML`.

## Quick start

```bash
pgreplkit -c config.yml discover      # what's in scope (read-only)
pgreplkit -c config.yml preflight     # eligibility & prerequisites (exit 10 on blocks)
pgreplkit -c config.yml setup --init-sync copy
pgreplkit -c config.yml watch         # until caught up
pgreplkit -c config.yml validate      # rows / objects / roles match?
pgreplkit -c config.yml cutover --writes-stopped
pgreplkit -c config.yml teardown --yes
```

Config can also come from the environment (`PGREPLKIT_SOURCE_HOST`, `..._TARGET_HOST`,
etc.). See the playbooks for full `config.yml` examples.

## Commands

| Command | Purpose |
|---|---|
| `discover` | Enumerate DBs/schemas/tables; in-scope vs skipped |
| `sizing` | Pre-replication planning: per-table size, index footprint & write rate |
| `plan` | Emit the balanced slot layout as editable YAML (seed for `manual`) |
| `preflight` | Read-only eligibility & prerequisite report |
| `globals` | Detect/recreate roles & tablespaces on the target |
| `setup` | Create publications/subscriptions/slots (init-sync strategies) |
| `refresh` | Add newly-appeared tables to replication |
| `status` / `watch` | Replication, initial-sync, and slot/WAL health |
| `validate` | Object/role/row comparison (`none`/`sampled`/`full`) |
| `ready` | Composite pass/fail cutover gate |
| `sync-sequences` | Copy sequence values source → target |
| `cutover` | Ordered quiesce → drain → sequences → validate → signal |
| `reverse` | Flip direction for rollback |
| `skip` | Skip a failing apply transaction (confirmation required) |
| `teardown` | Remove created artifacts (confirmation required) |
| `guide` | Generate a manual runbook; execute nothing |

Most mutating commands support `--dry-run` and `--generate-only`; destructive ones
require `--yes`. The exception is physical-seed setup (`snapshot-restore` /
`aurora-fast-clone`), which runs in execute mode only — use `guide` for its
review-and-run runbook. Exit codes are stable for CI gating (e.g. `preflight`=10,
`validate`=11, `ready`=12).

## Playbooks

- [Pre-provisioned green (existing clusters, `copy`)](docs/playbooks/01-pre-provisioned-green.md)
- [RDS-to-RDS (snapshot-restore seeded replication)](docs/playbooks/02-rds-to-rds.md)
- [Aurora-to-Aurora (fast-clone seeded replication)](docs/playbooks/03-aurora-to-aurora.md)
- [Reverse replication as part of cutover (rollback insurance)](docs/playbooks/04-reverse-as-part-of-cutover.md)
- [Aurora major-version upgrade via clone → upgrade → subscribe](docs/playbooks/05-aurora-major-version-upgrade.md)
- [Generate-only mode (`guide`): a review-and-run runbook](docs/playbooks/06-guide-generate-only.md)

Playbooks are transcribed from **actual verified runs** — a two-database local pair,
real RDS PostgreSQL 16, real Aurora PostgreSQL 16.4, and a real Aurora **17.7 → 18.3**
major-version upgrade (clone → upgrade → subscribe, with reverse rollback). Playbooks
1–3 also include a PostgreSQL 16 → 17 upgrade walkthrough.

## Design docs

- [REQUIREMENTS.md](REQUIREMENTS.md) — FR/NFR specification
- [DESIGN.md](DESIGN.md) — architecture and per-phase design

## Development

```bash
uv venv .venv && VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q -m "not integration"     # unit tests
.venv/bin/ruff check src tests                          # lint
```

Integration tests need a live PostgreSQL (Docker) and are gated on env vars
(`PGREPLKIT_IT_HOST`, `PGREPLKIT_IT_TGT_HOST`, …). Managed-engine paths are exercised by
the scripts under `scripts/` against real RDS.

## Status (v1.1)

Implemented and exercised on real infra: discovery, **sizing/activity planning**,
preflight (incl. source+target replication-parameter checks, target table/column
compatibility with generated-column parity, `REPLICA IDENTITY FULL` datatype caveat, and
source-write-block framing for missing replica identity), globals, setup (copy +
physical-seed with exactly-once seed-LSN resume), monitor, validate (row counts **and**
content checksums), cutover, reverse (writes-quiesced), teardown, `guide`, and
**partition spreading** (`--spread-partitions`, verified end-to-end on a partitioned
table across a local PostgreSQL 16 pair). `secret_ref` resolves from AWS Secrets
Manager, and `${VAR}` references in the config file expand from the environment.

Not yet active (planned, documented in REQUIREMENTS.md): cross-database **parallel**
execution (`setup` runs sequentially; `--concurrency` is not wired) — a performance
feature deferred so the single-writer manifest stays race-free.

## Safety

- Read-only phases open read-only sessions; mutating operations flow through one
  executor that supports execute / dry-run / generate-only.
- Configurable statement/lock timeouts protect the production source.
- Passwords are never logged; generated role passwords are written to a `chmod 600`
  file for DBA reference.
- Physical seeds are same-engine only (RDS→RDS, Aurora→Aurora); cross-engine
  (RDS↔Aurora) is out of scope due to LSN divergence — use `copy` or AWS DMS.
