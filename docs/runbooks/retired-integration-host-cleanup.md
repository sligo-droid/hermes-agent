# Retired Integration Host Cleanup

This repository change does not modify any host. Runtime configuration, services, installed skills, caches, indexes, memory-provider data, and local note archives remain unchanged until an operator performs a separately approved migration.

## Manual Checklist

- Confirm the repository change is approved and deployed before changing host state.
- Inventory every Hermes profile and explicitly configured memory host, including disabled and legacy aliases.
- Back up host-owned configuration and content using the operator's established recovery process.
- Remove retired integration settings, environment entries, installed skills, lock records, and cached catalog entries manually on each approved host.
- Refresh only the affected skill indexes and verify the retired capability cannot be searched, fetched, or installed.
- Validate each remaining memory host and profile independently before any optional gateway restart.
- Record the operator, affected hosts, validation evidence, and rollback location outside this repository.

## Stop Conditions

Stop without changing the host if any profile or host alias is unexplained, authoritative content has no verified destination or backup, a required service owner is unavailable, rollback cannot be demonstrated, or validation exposes unrelated drift.
