# Athena documentation

This directory is the canonical project documentation of the Athena monorepo. Write future implementation reports directly to `reports/`.

- [Architecture](ARCHITECTURE.md): monorepo layout, implemented runtime and component responsibilities.
- [Monorepo migration](MONOREPO_MIGRATION.md): source repositories and SHAs, file mapping, dependency changes, decisions.
- [Target architecture](VIXY_TARGET_ARCHITECTURE_EN.md): agreed design and roadmap.
- [Graph schema](GRAPH_SCHEMA.md): nodes, ownership, evidence and migrations.
- [Contracts](CONTRACTS.md): event schemas, identity, timestamps and provenance.
- [API](API.md): backend routes and CLI.
- [Frontend integration](FRONTEND_INTEGRATION.md): handoff for the app — starting the backend, endpoints, ids, notifications polling, simulator controls, CORS/auth, limitations.
- [System audit](VIXY_SYSTEM_AUDIT.md): historical baseline; findings are not current certification.
- [M1 report](reports/VIXY_M1_IMPLEMENTATION_REPORT.md): integration baseline.
- [M2 report](reports/VIXY_M2_IMPLEMENTATION_REPORT.md): intelligence baseline.
- [M3 report](reports/VIXY_M3_IMPLEMENTATION_REPORT.md): real local audiovisual perception and tested multi-day replay.
- [Monorepo validation](reports/VIXY_MONOREPO_VALIDATION.md): regression results of this repository against the M3 baseline.
- [MP4 input workflow](../demo/README.md): setup, manifests, replay and inspection.

The original M1/M2 report contents are preserved verbatim. Their publication status and limitations reflect the time they were written. On 2026-11-04, the user confirmed that the historical Baseten credential was revoked. This is user attestation, not an independent provider audit. The compromised key was not used or tested. That history is preserved in the original master-repo only; this repository was built from clean snapshots and does not contain it.

Release history before the monorepo: master-repo M1 included `feature/active-agent` commits `f0304ff` and `32e0676` (the agent M1 integrates). The seven milestone PRs (master-repo #1–#3, context-engine #1, dreamer-model #1–#3) were merged into each original repository's `main` in order M1 → M2 → M3, with merge commits. Those `main` commits are the sources of this monorepo; see [MONOREPO_MIGRATION.md](MONOREPO_MIGRATION.md).

Historical report filenames are preserved verbatim. References in M1/M2 report prose to target architecture/audit filenames resolve to the parent `docs/` directory; milestone report filenames resolve to `docs/reports/`. There are no separate authoritative workspace-root documents.

**Historical documents and the monorepo.** The M1–M3 reports, the system audit and the target architecture are preserved verbatim from the original repositories. In them:

- `master-repo` means this repository's root.
- `context-engine` and `dreamer-model` mean `components/context-engine` and `components/dreamer-model`.
- Setup commands that clone sibling repositories, the `VIXY_COMPONENTS_TOKEN` CI secret, and `yolomodelvision/` belong to the old layout. For the mapping, see [MONOREPO_MIGRATION.md](MONOREPO_MIGRATION.md).
