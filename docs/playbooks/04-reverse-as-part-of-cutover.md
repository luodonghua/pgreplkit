# Playbook 4 — Reverse replication as part of cutover (rollback insurance)

**When to use:** during a blue→green cutover you want a **safety net**: after switching
traffic to green, keep blue current by replicating green→blue, so you can roll back to
blue with minimal data loss if something goes wrong on green.

pgreplkit's `reverse` command flips an **in-sync** blue→green setup into green→blue. It
is performed from a consistent point (zero lag), tears down the forward direction to
avoid a replication loop, and wires the reverse direction with initial-sync `none`
(no re-copy needed — the clusters are identical at the swap point).

> This playbook is transcribed from an actual end-to-end run on a two-database
> PostgreSQL 16 pair (`shop`, `inventory`); the command output shown is real.

---

## Where reverse fits in the cutover

```
blue→green setup → monitor → validate → cutover(writes-stopped) → [SWITCH TRAFFIC]
                                                        │
                                                        └── reverse → green→blue CDC
                                                            (blue stays current for rollback)
```

The critical sequencing: **stop writes on blue, drain to zero lag, cut over, then
reverse.** Because reverse swaps from the zero-lag point, blue and green are identical
and no data is lost in the flip.

## Prerequisites

- A blue→green setup that is **in sync** (all slots synced, lag 0) — check with
  `pgreplkit ... ready`.
- Green must be reachable as a **publisher** from blue after the swap. In a config
  where the tool reaches each side differently from how the peers reach each other,
  set `advertised_host` on **both** endpoints (blue and green), e.g.:
  ```yaml
  source:  { host: 127.0.0.1, port: 55432, ..., advertised_host: blue-host, advertised_port: 5432 }
  target:  { host: 127.0.0.1, port: 55433, ..., advertised_host: green-host, advertised_port: 5432 }
  ```
  (After reverse, blue subscribes to green, so green's `advertised_host` must be
  reachable from blue.)
- Green needs the same replica-identity guarantees as any publisher (PKs / REPLICA
  IDENTITY on the tables being reversed).

## Steps (actual run)

```console
$ pgreplkit -c config.yml validate --depth sampled
│ OK    │ validate │ - │ source and target match │
0 block, 0 warn.

$ pgreplkit -c config.yml ready
READY — all slots synced, active, within lag threshold

# Stop application writes on blue, then:
$ pgreplkit -c config.yml cutover --writes-stopped
  ✓ quiesce: confirmed writes stopped on source
  ✓ drain: all slots synced at zero lag
  ✓ sequences: synced 0 sequence(s)
  ✓ validate: source and target match
  ✓ READY FOR CUTOVER: switch application traffic to the target now

# --- keep writes QUIESCED on green for the whole reverse swap ---
# Establish reverse (green → blue) as rollback insurance:
$ pgreplkit -c config.yml reverse --writes-stopped
  ✓ quiesce: confirmed writes stopped on the new source for the swap
  ✓ verified forward direction in-sync (zero lag)
  ✓ tore down forward direction
  ✓ established reverse direction (init-sync none): ...

# --- now open writes on green (traffic switch) ---
```

> **Important:** `reverse` requires `--writes-stopped` and assumes writes on the new
> source (green) stay quiesced for the *entire* swap. The reverse slot is created after
> the forward direction is torn down; any transaction committed on green between the
> in-sync check and the reverse slot's creation would be neither seeded nor captured —
> and thus lost on rollback. Quiesce green, run `reverse`, then open writes.

**Verify reverse CDC is live** — write on green, confirm it reaches blue:

```console
# write two rows on GREEN (now the source of truth)
$ psql "host=green ... dbname=shop" -c "INSERT INTO customers VALUES (9001,'A'),(9002,'B')"

# they appear on BLUE within a couple of seconds:
$ psql "host=blue ... dbname=shop" -c "SELECT name FROM customers WHERE id>=9001"
 reverse-test-A
 reverse-test-B

$ pgreplkit -c config.yml status        # now reports the reverse direction
│ shop      │ pgrk_… │ enabled │ 2/2 │ active │ reserved │ 0 │
│ inventory │ pgrk_… │ enabled │ 2/2 │ active │ reserved │ 0 │
```

(`status`, `validate`, `ready`, and `teardown` automatically operate on the reverse
direction after `reverse`, because pgreplkit records the direction in its manifest and
swaps the endpoints accordingly.)

## Rolling back to blue

If you need to abandon green and return to blue:
1. Stop writes on green.
2. Confirm reverse is caught up: `pgreplkit -c config.yml ready`.
3. Switch application traffic back to blue.
4. Tear down the reverse replication: `pgreplkit -c config.yml teardown --yes`.

Provided writes were quiesced on green across the `reverse` swap (above) and again
before this rollback, blue has every change committed on green, so rollback loses no
committed data. If green took writes *during* the reverse swap itself (i.e. `reverse`
was run without keeping green quiesced), those specific writes are not on blue.

## Finishing the migration (no rollback needed)

Once you're confident green is healthy and you no longer need the rollback path:

```console
$ pgreplkit -c config.yml teardown --yes
teardown complete.
```

This drops the reverse subscription (on blue), publication and slot (on green),
leaving both clusters clean.

---

## Notes & caveats

- **Loop avoidance:** `reverse` tears down the forward direction before creating the
  reverse one. pgreplkit refuses to run steady-state bidirectional replication; if you
  explicitly want it, that requires PG16 `origin = none` and is out of scope here.
- **No re-copy:** reverse uses initial-sync `none`. It relies on the forward direction
  being at **zero lag** when flipped — always run `ready`/`cutover` first.
- **Major-version reversals** (e.g. green 17 → blue 16) work for the data types in use
  but should be tested in non-prod; logical replication does not guarantee every 17-only
  feature round-trips to 16.
