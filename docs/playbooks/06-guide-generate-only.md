# Playbook 6 — Generate-only mode (`guide`): a runbook you review and run by hand

**When to use:** change-controlled environments where a tool may not connect and mutate
the databases directly. `pgreplkit guide` produces a complete, ordered runbook (SQL +
notes) and **executes nothing** — you attach it to a change ticket and a DBA runs the
steps. Secrets are redacted.

> The output below is a real capture from `pgreplkit guide` (engine auto-detected;
> connection password shown as `***`).

---

## What it does

- Runs the same planning as `setup` (discovery, slot layout, engine detection) but in
  **generate-only** mode: it may open **read-only** connections to tailor the runbook
  to your actual schema, and never issues a mutating statement.
- Emits per-database `CREATE PUBLICATION` / `CREATE SUBSCRIPTION` for the chosen slot
  layout, plus prerequisites, monitoring queries, ordered cutover, and teardown.
- Redacts the connection password in the emitted `CREATE SUBSCRIPTION`.

## Usage

```bash
pgreplkit -c config.yml guide --init-sync copy               > runbook.md
pgreplkit -c config.yml guide --init-sync snapshot-restore   > runbook-rds.md
pgreplkit -c config.yml guide --init-sync aurora-fast-clone  > runbook-aurora.md
```

`--slots {single|per-schema|balanced|manual}` and `--n N` control the slot layout in the
generated plan, exactly as for `setup`.

## Real captured output (`guide --init-sync copy`, balanced n=2)

````markdown
# pgreplkit — manual replication runbook

- source: `127.0.0.1:55432` (engine: vanilla)
- target: `127.0.0.1:55433`
- init-sync: `copy`  |  slots: `balanced`

## 1. Prerequisites
- Set `wal_level = logical` and restart the source.
- Ensure the target can reach the source (SG/firewall, pg_hba.conf).
- Pre-create the target schema (logical replication does not copy DDL):
  ```bash
  pg_dump --schema-only -h SOURCE -d DB | psql -h TARGET -d DB
  ```

## 2. Create publications & subscriptions
### database `shop`
```sql
-- source: create publication pgrk_guide_718d_shop_0 (2 tables)
CREATE PUBLICATION "pgrk_guide_718d_shop_0" FOR TABLE "public"."customers", "public"."orders"
  WITH (publish = 'insert, update, delete, truncate');
-- target: create subscription pgrk_guide_718d_shop_0 (copy_data=True)
CREATE SUBSCRIPTION "pgrk_guide_718d_shop_0"
  CONNECTION 'host=pgrepl-src port=5432 user=postgres dbname=shop password=***'
  PUBLICATION "pgrk_guide_718d_shop_0"
  WITH (copy_data = true, create_slot = true, enabled = true,
        slot_name = 'pgrk_guide_718d_shop_0', disable_on_error = true);
```

## 3. Monitor & validate
```sql
SELECT slot_name, pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS lag,
       active, wal_status FROM pg_replication_slots;
SELECT srsubstate, count(*) FROM pg_subscription_rel GROUP BY 1;
```

## 4. Cutover (ordered)
1. Stop writes on the source.
2. Wait until lag reaches **0** on all slots.
3. Sync sequences.
4. Validate row counts / object counts match.
5. Switch application traffic to the target.

## 5. Teardown
... DROP SUBSCRIPTION / DROP PUBLICATION per slot ...
````

Notes:
- The FK-related `customers` + `orders` are grouped into **one** slot by the balanced
  planner (FK-affinity), so they replicate in a single consistent stream.
- For RDS/Aurora, the prerequisites section instead calls out
  `rds.logical_replication = 1` (parameter group, static → reboot); for physical-seed
  strategies it adds the snapshot/clone AWS CLI and the
  `aurora_volume_logical_start_lsn()` / `rds_tools.logical_seed_lsn()` seed capture.

## Verifying no execution happened

`guide` opens only read-only sessions and issues no `CREATE`/`ALTER`/`DROP`. You can
confirm afterward that the source has no new publication/slot and the target has no new
subscription — the runbook is text only.
