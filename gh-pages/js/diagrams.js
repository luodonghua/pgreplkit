/* ==========================================================================
   diagrams.js — Reusable Mermaid flowchart templates.

   Content fragments request a diagram with:
       <div data-diagram="KEY" data-src="Blue (PG16)" data-tgt="Green (PG16)"></div>

   The renderer replaces that element's content with a rendered Mermaid graph.
   Centralising the flowcharts here means a fix to one flow shape updates every
   scenario that uses it. Add a new shape by adding a key to DIAGRAMS.

   Each template is a function (opts) => mermaidDefinitionString, where opts is
   the element's dataset (data-* attributes), so a fragment can label the nodes.
   ========================================================================== */

(function (global) {
  "use strict";

  const d = (opts, key, fallback) => (opts && opts[key]) ? opts[key] : fallback;

  const DIAGRAMS = {
    /* ---- Logical initial COPY (version-independent, any platform) ---- */
    "logical-copy": (o) => `flowchart TD
      A["Enable logical WAL on source<br/>(wal_level=logical / rds.logical_replication=1)"] --> B["Pre-create schema on target<br/>(pg_dump --schema-only | psql)"]
      B --> C["CREATE PUBLICATION on source"]
      C --> D["CREATE SUBSCRIPTION on target<br/>(copy_data = true)"]
      D --> E["Initial COPY runs<br/>existing rows loaded"]
      E --> F["Streaming CDC<br/>live changes applied"]
      F --> G{"Lag = 0 and<br/>row counts match?"}
      G -- no --> F
      G -- yes --> H["Stop writes on source"]
      H --> I["Sync sequences"]
      I --> J["Validate"]
      J --> K["Switch app traffic to target"]
      style A fill:#e3f2fd,stroke:#1565c0
      style K fill:#e8f5e9,stroke:#2e7d32
      style H fill:#fff8e1,stroke:#b26a00`,

    /* ---- pg_dump / pg_restore manual seed, then attach CDC ---- */
    "pgdump-seed": (o) => `flowchart TD
      A["Enable logical WAL on source"] --> B["CREATE PUBLICATION on source"]
      B --> C["Create logical slot on source<br/>(pin WAL from restart_lsn)"]
      C --> D["pg_dump source (data) at/after slot point"]
      D --> E["pg_restore into target"]
      E --> F["CREATE SUBSCRIPTION on target<br/>(copy_data = false, create_slot = false,<br/>slot_name = existing slot)"]
      F --> G["Streaming CDC resumes from slot"]
      G --> H{"Lag = 0 and<br/>counts match?"}
      H -- no --> G
      H -- yes --> I["Stop writes -> sequences -> validate"]
      I --> J["Switch app traffic to target"]
      style A fill:#e3f2fd,stroke:#1565c0
      style J fill:#e8f5e9,stroke:#2e7d32
      style I fill:#fff8e1,stroke:#b26a00`,

    /* ---- RDS snapshot restore physical seed (RDS -> RDS) ---- */
    "physical-seed-rds": (o) => `flowchart TD
      A["Enable rds.logical_replication on source<br/>(parameter group, reboot)"] --> B["CREATE PUBLICATION on source"]
      B --> C["Create logical slot on source<br/>(publication FIRST, then slot)"]
      C --> D["Take RDS snapshot<br/>(contains publication + data)"]
      D --> E["Restore GREEN instance from snapshot"]
      E --> F["On green: rds_tools.logical_seed_lsn()<br/>-> seed LSN"]
      F --> G["CREATE SUBSCRIPTION on green<br/>(copy_data=false, create_slot=false, enabled=false)"]
      G --> H["pg_replication_origin_advance(pg_&lt;oid&gt;, seed_lsn)<br/>= exactly-once boundary"]
      H --> I["ALTER SUBSCRIPTION ... ENABLE"]
      I --> J["Streaming CDC from seed LSN"]
      J --> K{"Lag = 0 and<br/>counts match?"}
      K -- no --> J
      K -- yes --> L["Stop writes -> sequences -> validate -> cutover"]
      style A fill:#e3f2fd,stroke:#1565c0
      style H fill:#fff3e0,stroke:#e65100
      style L fill:#e8f5e9,stroke:#2e7d32`,

    /* ---- Aurora fast clone physical seed (Aurora -> Aurora) ---- */
    "physical-seed-aurora": (o) => `flowchart TD
      A["Enable rds.logical_replication on source<br/>(cluster parameter group, reboot)"] --> B["CREATE PUBLICATION on source"]
      B --> C["Create logical slot on source<br/>(publication FIRST, then slot)"]
      C --> D["Fast-clone the cluster<br/>(copy-on-write, instant)"]
      D --> E["Add a writer instance to the clone"]
      E --> F["On clone: aurora_volume_logical_start_lsn()<br/>-> seed LSN"]
      F --> G["CREATE SUBSCRIPTION on clone<br/>(copy_data=false, create_slot=false, enabled=false)"]
      G --> H["pg_replication_origin_advance(pg_&lt;oid&gt;, seed_lsn)<br/>= exactly-once boundary"]
      H --> I["ALTER SUBSCRIPTION ... ENABLE"]
      I --> J["Streaming CDC from seed LSN"]
      J --> K{"Lag = 0 and<br/>counts match?"}
      K -- no --> J
      K -- yes --> L["Stop writes -> sequences -> validate -> cutover"]
      style A fill:#e3f2fd,stroke:#1565c0
      style H fill:#fff3e0,stroke:#e65100
      style L fill:#e8f5e9,stroke:#2e7d32`,

    /* ---- Cross-cloud (GCP/Azure -> AWS) networking + logical ---- */
    "cross-cloud": (o) => `flowchart LR
      subgraph SRC["${d(o, "srcCloud", "Source cloud")}"]
        P["${d(o, "src", "Source PostgreSQL")}<br/>PUBLISHER"]
      end
      subgraph NET["Network path"]
        V["Public IP + firewall/authorized nets<br/>OR VPN / Interconnect"]
      end
      subgraph AWS["AWS VPC"]
        S["${d(o, "tgt", "Target (RDS/Aurora)")}<br/>SUBSCRIBER"]
      end
      P --> V --> S
      S -. "subscriber pulls WAL<br/>from publisher :5432" .-> P
      style P fill:#e3f2fd,stroke:#1565c0
      style S fill:#e8f5e9,stroke:#2e7d32
      style V fill:#fff8e1,stroke:#b26a00`,

    /* ---- Bi-directional (active-active) with origin=none ---- */
    "bidirectional": (o) => `flowchart LR
      subgraph NA["Node A (PG16+)"]
        PA["Publication A"]
        SA["Subscription A<br/>(origin = none)"]
      end
      subgraph NB["Node B (PG16+)"]
        PB["Publication B"]
        SB["Subscription B<br/>(origin = none)"]
      end
      PA -- "A's local changes" --> SB
      PB -- "B's local changes" --> SA
      SA -. "origin=none: do NOT<br/>re-publish replicated rows" .-> PA
      SB -. "origin=none: do NOT<br/>re-publish replicated rows" .-> PB
      style PA fill:#e3f2fd,stroke:#1565c0
      style PB fill:#e3f2fd,stroke:#1565c0
      style SA fill:#e8f5e9,stroke:#2e7d32
      style SB fill:#e8f5e9,stroke:#2e7d32`,

    /* ---- Where cutover + optional reverse fit ---- */
    "cutover-reverse": (o) => `flowchart TD
      A["Blue -> Green in sync (lag 0)"] --> B["Stop writes on Blue"]
      B --> C["Drain to zero lag"]
      C --> D["Sync sequences"]
      D --> E["Validate counts match"]
      E --> F["Switch app traffic to Green"]
      F --> G{"Want rollback<br/>insurance?"}
      G -- yes --> H["reverse: Green -> Blue CDC<br/>(keep Blue current)"]
      G -- no --> I["Teardown replication"]
      H --> I
      style B fill:#fff8e1,stroke:#b26a00
      style F fill:#e8f5e9,stroke:#2e7d32`
  };

  function renderInto(el) {
    const key = el.getAttribute("data-diagram");
    const tmpl = DIAGRAMS[key];
    if (!tmpl) {
      el.innerHTML = `<em>(diagram "${key}" not found)</em>`;
      return;
    }
    const def = tmpl(el.dataset);
    const wrap = document.createElement("div");
    wrap.className = "mermaid";
    wrap.textContent = def;
    el.innerHTML = "";
    el.classList.add("diagram-wrap");
    el.appendChild(wrap);
  }

  function renderAll(root) {
    const nodes = (root || document).querySelectorAll("[data-diagram]");
    nodes.forEach(renderInto);
    if (global.mermaid && nodes.length) {
      try {
        global.mermaid.initialize({ startOnLoad: false, theme: "default", securityLevel: "loose" });
        global.mermaid.run({ nodes: (root || document).querySelectorAll(".mermaid") });
      } catch (e) {
        // Older mermaid API fallback
        try { global.mermaid.init(undefined, (root || document).querySelectorAll(".mermaid")); }
        catch (e2) { /* leave text */ }
      }
    }
  }

  global.DIAGRAMS = DIAGRAMS;
  global.DiagramUtil = { renderAll, renderInto };
})(window);
