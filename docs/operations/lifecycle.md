# Managed lifecycle and recovery

Use the same private environment file and the same operator account for every
operation. This guide covers the universal hub and the managed split topology;
sections that apply to only one topology say so explicitly. The Unraid profile
runs as root; `compose-existing` may use a trusted Docker-capable account with a
private primary group.

## Verify

Run the read-only verifier after install, host or Docker restart, upgrade and
rollback:

```bash
./btctl doctor --env /absolute/private/path/deployment.env
```

It checks source/image identity, private state, exact reader evidence, role labels,
health, authentication, networks, ports and generated artifacts. Treat any
failed or missing check as a failed deployment.

## Recover lost state

For a split deployment only, if `state.json` was lost, `adopt` can reconstruct
it from an exact healthy, already-labelled runtime:

```bash
./btctl adopt --env /absolute/private/path/cwa-translate.env
```

Adoption does not change Docker. It rejects unlabeled, partial, insecure or
ambiguous containers and routes a combined legacy container to the migration
workflow instead of relabeling it.

## Change providers

The universal hub treats all enabled readers as one restart and security
boundary. Keep the exact old environment, create a separate mode-`0600`
replacement, then run:

```bash
./btctl uninstall --env /absolute/private/path/old-hub.env --yes
./btctl plan --env /absolute/private/path/new-hub.env
./btctl install --env /absolute/private/path/new-hub.env --yes
./btctl doctor --env /absolute/private/path/new-hub.env
```

Translation databases and backups remain in place, while hub-owned reader
session keys are regenerated. Refresh or sign in again in open reader tabs. If
the replacement cannot pass `doctor`, use its environment to remove a committed
replacement, reinstall the retained old environment, and verify it before
resuming use.

For a split deployment, create a second mode-`0600` environment with the same
runtime/reader topology and only provider values changed. First run without
`--yes` to print a redacted plan, then confirm:

```bash
./btctl reconfigure --env /absolute/private/path/new-provider.env
./btctl reconfigure --env /absolute/private/path/new-provider.env --yes
./btctl doctor --env /absolute/private/path/new-provider.env
```

The transaction replaces only the API container or Compose service. Private
old/new snapshots are digest-bound to a secret-free journal. All configured
providers, reader authentication and SQLite are probed before new state is
published. A normal failure restores the old API automatically. If the process
or host stops mid-cutover, rerun the same confirmed command: it validates the
journal, restores the old role and then performs a fresh transaction. Do not
edit or delete `reconfigure.json` or its private snapshots. If automatic
rollback itself fails, preserve those files and collect `doctor`/Docker inspect
evidence before changing runtime state.

## Upgrade from the combined v2.1.4 runtime

Migration supports one exact v2.1.4 container. Keep its live data separate from
the new `BT_DATA_DIR`, and set:

```dotenv
BT_LEGACY_CONTAINER=book-translator-v214-rollback
BT_LEGACY_DATA_DIR=/absolute/path/to/legacy-data
```

A source-built legacy container must use the `2.1.4` or `v2.1.4` image tag.
For compatibility with the historical Community Applications template, the
exact image reference
`ghcr.io/felixapel/cwa-ebook-translate-plugin:latest` is also recognized, but
only when a network-disabled, read-only probe of the container's immutable
image ID reports `/app/VERSION` as `2.1.4`. The verified image ID is recorded in
the migration journal. `btctl` does not pull or resolve the mutable tag during
this check, and every other `latest` reference fails closed.

Then run:

```bash
./btctl upgrade --env /absolute/private/path/cwa-translate.env --yes
./btctl doctor --env /absolute/private/path/cwa-translate.env
```

The upgrade stops the only writer, checkpoints the SQLite WAL, validates the
source, creates an external snapshot, copies into a new empty target, validates
the copy and keeps the exact legacy container/image restartable. The private
journal makes interrupted preparation, snapshot and cutover steps retryable.
Never point a fresh install at the live legacy directory or run both versions
against one database.

## Roll back a migration

If browser acceptance fails:

```bash
./btctl rollback --env /absolute/private/path/cwa-translate.env --yes
```

Rollback restores and health-checks the exact preserved legacy runtime. It uses
the journal's authoritative paths, not mutable environment values. Missing or
corrupt target data does not prevent legacy recovery, but the journal records
the target as unavailable and blocks automatic re-upgrade until repaired.

## Uninstall and reinstall

Remove only resources owned by the recorded install:

```bash
./btctl uninstall --env /absolute/private/path/cwa-translate.env --yes
```

The retryable operation preserves the stock reader, external networks, local
image, translation database, backups and final state evidence. It removes the
installation-owned `reader_session_key`, so outstanding opaque browser sessions
cannot survive an uninstall. The sibling `tenant_key` stays with the
translation database, so each reader's cache and glossary tenants survive
reinstall and upgrade; delete it together with the database to start over
deliberately. Reinstalling the same identity and data path after
a completed uninstall is supported; a successful reinstall creates a fresh key
and moves the prior final record into `BT_STATE_DIR/history/`. Active, partial
or mismatched state is never overwritten.

New split CWA and Kavita installations have independent schema-3 state and
connector UUIDs. The universal hub records one schema-3 topology while retaining
separate reader data, keys and cookies below its owned root. Never reuse one
split install's state/data paths for the other. Schema-1 CWA and former
inline-Compose schema-2 state remain readable for lifecycle compatibility, but
cannot be relabeled or provider-reconfigured in place.
Changing reader type is a separate install/uninstall operation, not an in-place
upgrade.

## Failure behavior

- Invalid source identity stops before an operator receives the Docker socket.
- Invalid configuration, reader evidence, names, paths or network preconditions
  stops before runtime creation.
- A failed startup removes only newly created translator containers and their
  private network and newly created session key; the reader, external networks,
  translation database, configuration and backups stay. A successful cleanup
  leaves exact `cleaned` retry evidence, so rerunning the unchanged install is
  supported even though the data directory is now nonempty. Any plan mismatch
  or cleanup error remains fail-closed.
- The universal hub applies the same journal before image/data/runtime mutation.
  It removes only per-reader session keys first created by that failed attempt;
  existing keys and both translation databases remain. A cleanup failure leaves
  private `cleanup-failed` evidence and reports every bounded cleanup error
  instead of permitting an ambiguous retry. Compose operators do not need host
  access to UID-101 reader trees: key presence, ownership and cleanup are checked
  by exact network-disabled Docker helpers. If state committed but final journal
  removal failed, successful `doctor` remains read-only; the matching `uninstall`
  reconciles only exact `starting` evidence before a later reinstall.
- State is committed only after live postconditions pass.
- Do not recover by exposing the API, setting disabled auth, editing generated
  state or applying generated Unraid templates manually.

For initial configuration and profile-specific prerequisites, see the
[managed install guide](../install/btctl.md). For symptoms and evidence
collection, see [troubleshooting](troubleshooting.md).
