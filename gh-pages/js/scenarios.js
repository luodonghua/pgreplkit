/* ==========================================================================
   scenarios.js — Central, EXTENSIBLE registry for the replication guide.

   HOW TO EXTEND (add a new source, target, sync method, or scenario):

   1. ADD A SOURCE ENGINE:
        Add an entry to REGISTRY.sources. Give it a stable `id` (used in URLs
        and content filenames), a display `label`, a `platform` tag, and the
        `pgVersions` it supports.

   2. ADD A TARGET ENGINE:
        Add an entry to REGISTRY.targets (same shape as a source).

   3. ADD A SYNC METHOD:
        Add an entry to REGISTRY.syncMethods with an `appliesWhen(ctx)` guard
        describing when it is valid (based on source/target).

   4. DEFINE VALIDITY:
        The single source of truth for "is this combination valid?" is
        isValidCombination(). It composes small rules. Add a rule function to
        RULES and it is automatically applied. Each rule returns either
        `null` (ok) or a string explaining why the combo is rejected.

   5. ADD THE CONTENT PAGE:
        Content lives in /content/<contentKey>.html where contentKey is
        produced by contentKeyFor(ctx). By default that is
        `<source.id>-to-<target.id>`. A scenario can override with an explicit
        `contentKey` in SCENARIO_OVERRIDES (e.g. bi-directional).

   Nothing else in the app hard-codes the list of engines — index.html,
   app.js and guide-renderer.js all read from this registry.
   ========================================================================== */

(function (global) {
  "use strict";

  /* ---- Supported PostgreSQL major versions this guide targets ---- */
  const PG_VERSIONS = {
    min: 10,   // logical replication core (pgoutput) since PG 10
    max: 17,   // latest major covered by this guide
    // Feature availability by major version — referenced in prerequisites.
    features: {
      logicalReplication: 10,     // CREATE PUBLICATION / SUBSCRIPTION
      publishTruncate: 11,        // publish TRUNCATE
      columnLists: 15,            // publication column lists
      rowFilters: 15,             // publication WHERE row filters
      twoPhase: 15,               // streaming of two-phase (prepared) txns
      originFilter: 16,           // subscription `origin = none` (loop-safe bi-directional)
      nonSuperuserSubscribe: 16,  // pg_create_subscription predefined role
      failoverSlots: 17           // failover-aware logical slots
    }
  };

  /* ==========================================================================
     OFFICIAL DOCUMENTATION LINKS
     Used to build the "References" section at the end of every guide so users
     can cross-check what we say against the authoritative sources. All URLs
     were verified to resolve to their official domains.
     ========================================================================== */
  const PG_DOCS = {
    overview:     { label: "PostgreSQL manual — Logical Replication (chapter)",            url: "https://www.postgresql.org/docs/current/logical-replication.html" },
    createPub:    { label: "PostgreSQL manual — CREATE PUBLICATION",                       url: "https://www.postgresql.org/docs/current/sql-createpublication.html" },
    createSub:    { label: "PostgreSQL manual — CREATE SUBSCRIPTION (incl. origin option)", url: "https://www.postgresql.org/docs/current/sql-createsubscription.html" },
    restrictions: { label: "PostgreSQL manual — Logical Replication Restrictions",         url: "https://www.postgresql.org/docs/current/logical-replication-restrictions.html" },
    config:       { label: "PostgreSQL manual — Logical Replication Configuration Settings", url: "https://www.postgresql.org/docs/current/logical-replication-config.html" }
  };

  /* ==========================================================================
     SOURCE ENGINES
     ========================================================================== */
  const sources = [
    {
      id: "self-managed",
      label: "Self-Managed PostgreSQL",
      platform: "on-prem / EC2 / VM",
      short: "Self-Managed",
      pgVersions: { min: 10, max: 17 },
      // How logical WAL is enabled on this platform (shown in prerequisites).
      logicalWal: {
        method: "parameter",
        detail: "Set <code>wal_level = logical</code> in postgresql.conf and restart."
      },
      privileges: "SUPERUSER (PG &lt; 16) or a role with REPLICATION for slot creation.",
      canBeSource: true
    },
    {
      id: "rds-pg",
      label: "Amazon RDS for PostgreSQL",
      docUrl: { label: "Amazon RDS — Performing logical replication for RDS for PostgreSQL", url: "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.FeatureSupport.LogicalReplication.html" },
      platform: "AWS RDS",
      short: "RDS PostgreSQL",
      pgVersions: { min: 10, max: 17 },
      logicalWal: {
        method: "parameter-group",
        detail: "Set <code>rds.logical_replication = 1</code> in the DB parameter group (static — requires reboot)."
      },
      privileges: "Grant <code>rds_replication</code> to the migration role.",
      canBeSource: true
    },
    {
      id: "aurora-pg",
      label: "Amazon Aurora PostgreSQL",
      docUrl: { label: "Amazon Aurora — Using PostgreSQL logical replication with Aurora", url: "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Replication.Logical.html" },
      platform: "AWS Aurora",
      short: "Aurora PostgreSQL",
      pgVersions: { min: 11, max: 16 },
      logicalWal: {
        method: "cluster-parameter-group",
        detail: "Set <code>rds.logical_replication = 1</code> in the DB <em>cluster</em> parameter group (static — reboot)."
      },
      privileges: "Grant <code>rds_replication</code> to the migration role.",
      canBeSource: true
    },
    {
      id: "cloudsql",
      label: "Google Cloud SQL for PostgreSQL",
      docUrl: { label: "Google Cloud SQL — Set up logical replication and decoding", url: "https://cloud.google.com/sql/docs/postgres/replication/configure-logical-replication" },
      platform: "GCP Cloud SQL",
      short: "Cloud SQL",
      pgVersions: { min: 10, max: 16 },
      logicalWal: {
        method: "flag",
        detail: "Set the <code>cloudsql.logical_decoding = on</code> flag (and <code>cloudsql.enable_pglogical</code> if using pglogical). Requires instance restart."
      },
      privileges: "Grant <code>cloudsqlsuperuser</code> / <code>REPLICATION</code> as documented by Google.",
      canBeSource: true,
      egressNote: "Cloud SQL is the PUBLISHER here; the AWS target subscriber must reach Cloud SQL's IP over the network (public IP + authorized networks, or a VPN/Interconnect for private IP)."
    },
    {
      id: "alloydb",
      label: "Google AlloyDB for PostgreSQL",
      docUrl: { label: "Google AlloyDB — Configure an instance's database flags (logical decoding)", url: "https://cloud.google.com/alloydb/docs/instance-configure-database-flags" },
      platform: "GCP AlloyDB",
      short: "AlloyDB",
      pgVersions: { min: 14, max: 16 },
      logicalWal: {
        method: "flag",
        detail: "Set the <code>alloydb.logical_decoding = on</code> database flag. Requires instance restart."
      },
      privileges: "Use the <code>alloydbsuperuser</code> role (or a role granted REPLICATION).",
      canBeSource: true,
      egressNote: "AlloyDB only exposes a PRIVATE IP. The AWS target subscriber must reach it through a VPN or Cloud Interconnect between GCP and AWS."
    },
    {
      id: "azure-pg",
      label: "Azure Database for PostgreSQL (Flexible Server)",
      docUrl: { label: "Azure Database for PostgreSQL Flexible Server — Logical replication and decoding", url: "https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-logical" },
      platform: "Azure",
      short: "Azure PostgreSQL",
      pgVersions: { min: 11, max: 16 },
      logicalWal: {
        method: "server-parameter",
        detail: "Set <code>wal_level = logical</code> (server parameter) and set <code>max_replication_slots</code>/<code>max_wal_senders</code>; requires a server restart."
      },
      privileges: "Use the <code>azure_pg_admin</code> role; grant REPLICATION to the migration role.",
      canBeSource: true,
      egressNote: "Azure Flexible Server is the PUBLISHER. The AWS subscriber must reach it via public access + firewall rules, or a site-to-site VPN for private access."
    }
  ];

  /* ==========================================================================
     TARGET ENGINES  (currently the two requested AWS targets)
     ========================================================================== */
  const targets = [
    {
      id: "rds-pg",
      label: "Amazon RDS for PostgreSQL",
      docUrl: { label: "Amazon RDS — Performing logical replication for RDS for PostgreSQL", url: "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.FeatureSupport.LogicalReplication.html" },
      platform: "AWS RDS",
      short: "RDS PostgreSQL",
      pgVersions: { min: 10, max: 17 },
      subscriberPrivileges: "Role with <code>rds_superuser</code> (PG &lt; 16) or <code>rds_replication</code> + <code>pg_create_subscription</code> (PG 16+).",
      canBeTarget: true
    },
    {
      id: "aurora-pg",
      label: "Amazon Aurora PostgreSQL",
      docUrl: { label: "Amazon Aurora — Using PostgreSQL logical replication with Aurora", url: "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Replication.Logical.html" },
      platform: "AWS Aurora",
      short: "Aurora PostgreSQL",
      pgVersions: { min: 11, max: 16 },
      subscriberPrivileges: "Role with <code>rds_superuser</code> (PG &lt; 16) or <code>rds_replication</code> + <code>pg_create_subscription</code> (PG 16+).",
      canBeTarget: true
    }
  ];

  /* ==========================================================================
     INITIAL DATA SYNC METHODS
     `appliesWhen(ctx)` returns true if the method is selectable for the given
     {source, target}. This is what greys-out invalid dropdown options.
     ========================================================================== */
  const syncMethods = [
    {
      id: "replication-copy",
      label: "Replication Copy (built-in COPY)",
      short: "copy",
      // Works for every source→target pair: version-independent logical copy.
      appliesWhen: () => true,
      versionIndependent: true,
      blurb: "PostgreSQL's built-in initial data copy (copy_data = true). Works across major versions and every platform. Best for small/medium datasets."
    },
    {
      id: "pg-dump",
      label: "pg_dump / pg_restore (manual seed)",
      short: "pg_dump",
      appliesWhen: () => true,
      versionIndependent: true,
      blurb: "Seed the target with pg_dump/pg_restore, then attach logical replication with copy_data = false. Works everywhere; good when you want control over the load (parallelism, indexes-after-load)."
    },
    {
      id: "rds-snapshot",
      label: "RDS Snapshot Restore (physical seed)",
      short: "snapshot-restore",
      // Physical seed only valid RDS -> RDS (same platform preserves LSN continuity).
      appliesWhen: (ctx) => ctx.source === "rds-pg" && ctx.target === "rds-pg",
      versionIndependent: false,
      blurb: "Fast physical seed via an RDS snapshot restore that preserves LSN continuity (rds_tools.logical_seed_lsn), then exactly-once logical CDC. Same major version only."
    },
    {
      id: "aurora-clone",
      label: "Aurora Fast Clone (copy-on-write seed)",
      short: "aurora-fast-clone",
      appliesWhen: (ctx) => ctx.source === "aurora-pg" && ctx.target === "aurora-pg",
      versionIndependent: false,
      blurb: "Instant copy-on-write clone (aurora_volume_logical_start_lsn) regardless of data size, then exactly-once logical CDC. Same major version only."
    }
  ];

  /* ==========================================================================
     WHAT TO REPLICATE (scope)
     ========================================================================== */
  const scopes = [
    {
      id: "schemas",
      label: "Entire Schemas (all tables in one or more schemas)",
      short: "schemas",
      minVersion: 15, // FOR TABLES IN SCHEMA requires PG 15+
      blurb: "Publish every table in chosen schemas with FOR TABLES IN SCHEMA (PG 15+). On older versions, fall back to FOR ALL TABLES or an explicit table list."
    },
    {
      id: "tables",
      label: "Specific Tables",
      short: "tables",
      minVersion: 10,
      blurb: "Publish an explicit list of tables with FOR TABLE. Works on all supported versions and lets you use column lists / row filters (PG 15+)."
    }
  ];

  /* ==========================================================================
     SCENARIO OVERRIDES — special scenarios that are not a plain source→target
     matrix cell (e.g. bi-directional). These appear as extra "featured"
     scenarios on the landing page and map to their own content file.
     ========================================================================== */
  const specialScenarios = [
    {
      id: "bidirectional",
      contentKey: "bidirectional",
      label: "Bi-Directional Replication (active-active)",
      description: "Two PostgreSQL nodes each publishing and subscribing to the other, using the PG16 <code>origin = none</code> filter to prevent replication loops.",
      minVersion: 16,
      tags: ["PG 16+", "origin = none", "advanced"],
      requiresFeature: "originFilter",
      docs: [
        { label: "PostgreSQL manual — Logical Replication (chapter)", url: "https://www.postgresql.org/docs/current/logical-replication.html" },
        { label: "PostgreSQL manual — CREATE SUBSCRIPTION (origin = none)", url: "https://www.postgresql.org/docs/current/sql-createsubscription.html" },
        { label: "PostgreSQL manual — Logical Replication Configuration Settings", url: "https://www.postgresql.org/docs/current/logical-replication-config.html" }
      ]
    }
  ];

  /* ==========================================================================
     VALIDITY RULES
     Each rule: (ctx) => null (ok) | string (reason it is invalid).
     ctx = { source, target, sync, scope } of engine/method IDs.
     ========================================================================== */
  const RULES = [
    function requireCoreSelections(ctx) {
      // Only validate once source + target + sync are chosen.
      if (!ctx.source || !ctx.target || !ctx.sync) return "__incomplete__";
      return null;
    },

    function syncMethodApplies(ctx) {
      const m = byId(syncMethods, ctx.sync);
      if (!m) return "Unknown sync method.";
      if (!m.appliesWhen({ source: ctx.source, target: ctx.target })) {
        const s = byId(sources, ctx.source);
        const t = byId(targets, ctx.target);
        return `"${m.label}" is not valid for ${s ? s.short : ctx.source} → ${t ? t.short : ctx.target}. ` +
               physicalSeedHint(m, s, t);
      }
      return null;
    },

    function physicalSeedSamePlatform(ctx) {
      // Physical-seed methods require identical platform AND same major version.
      const m = byId(syncMethods, ctx.sync);
      if (!m || m.versionIndependent) return null;
      if (m.id === "rds-snapshot" && !(ctx.source === "rds-pg" && ctx.target === "rds-pg")) {
        return "RDS Snapshot Restore only works RDS → RDS on the same major version. Use Replication Copy or pg_dump for cross-platform or cross-version.";
      }
      if (m.id === "aurora-clone" && !(ctx.source === "aurora-pg" && ctx.target === "aurora-pg")) {
        return "Aurora Fast Clone only works Aurora → Aurora on the same major version. Use Replication Copy or pg_dump for cross-platform or cross-version.";
      }
      return null;
    },

    function crossCloudNeedsLogical(ctx) {
      // GCP/Azure sources into AWS targets can only use logical (copy/pg_dump).
      const s = byId(sources, ctx.source);
      const m = byId(syncMethods, ctx.sync);
      const crossCloud = s && ["cloudsql", "alloydb", "azure-pg"].includes(s.id);
      if (crossCloud && m && !m.versionIndependent) {
        return `${s.short} → AWS is a cross-cloud move; physical seeds (snapshot/clone) do not apply. Use Replication Copy or pg_dump.`;
      }
      return null;
    }
  ];

  /* ==========================================================================
     PUBLIC API
     ========================================================================== */
  function byId(list, id) { return list.find((x) => x.id === id) || null; }

  function physicalSeedHint(method, source, target) {
    if (method.id === "rds-snapshot") return "It requires both sides to be RDS PostgreSQL (same major version).";
    if (method.id === "aurora-clone") return "It requires both sides to be Aurora PostgreSQL (same major version).";
    return "";
  }

  // Returns { valid: bool, reason: string|null, incomplete: bool }
  function isValidCombination(ctx) {
    for (const rule of RULES) {
      const res = rule(ctx);
      if (res === "__incomplete__") return { valid: false, reason: null, incomplete: true };
      if (res) return { valid: false, reason: res, incomplete: false };
    }
    return { valid: true, reason: null, incomplete: false };
  }

  // Which sync methods are usable for the chosen source/target (for greying out).
  function availableSyncMethods(ctx) {
    return syncMethods.map((m) => ({
      id: m.id,
      label: m.label,
      enabled: (!ctx.source || !ctx.target) ? true : m.appliesWhen({ source: ctx.source, target: ctx.target })
    }));
  }

  // Content file key: default `<source>-to-<target>`, overridable for specials.
  function contentKeyFor(ctx) {
    if (ctx.special) return ctx.special; // e.g. "bidirectional"
    if (!ctx.source || !ctx.target) return null;
    return `${ctx.source}-to-${ctx.target}`;
  }

  // The minimum PG version required by the whole selection (max of all mins).
  function requiredPgVersion(ctx) {
    let min = PG_VERSIONS.features.logicalReplication;
    const scope = byId(scopes, ctx.scope);
    if (scope) min = Math.max(min, scope.minVersion);
    return min;
  }

  // Official-source links (1–5) for the "References" section of a guide.
  function referencesFor(ctx) {
    const out = [];
    const seen = new Set();
    const add = (d) => { if (d && d.url && !seen.has(d.url)) { seen.add(d.url); out.push(d); } };

    if (ctx && ctx.special) {
      const sp = specialScenarios.find((s) => s.id === ctx.special);
      ((sp && sp.docs) || [PG_DOCS.overview, PG_DOCS.createSub]).forEach(add);
      return out.slice(0, 5);
    }

    // Matrix scenario: core PG + source platform + target platform + key command + restrictions.
    add(PG_DOCS.overview);
    const s = byId(sources, ctx.source);
    const t = byId(targets, ctx.target);
    if (s && s.docUrl) add(s.docUrl);
    if (t && t.docUrl) add(t.docUrl);
    add(PG_DOCS.createSub);
    add(PG_DOCS.restrictions);
    return out.slice(0, 5);
  }

  global.REGISTRY = {
    PG_VERSIONS,
    sources,
    targets,
    syncMethods,
    scopes,
    specialScenarios,
    // helpers
    byId,
    isValidCombination,
    availableSyncMethods,
    contentKeyFor,
    requiredPgVersion,
    referencesFor,
    PG_DOCS,
    getSource: (id) => byId(sources, id),
    getTarget: (id) => byId(targets, id),
    getSyncMethod: (id) => byId(syncMethods, id),
    getScope: (id) => byId(scopes, id)
  };
})(window);
