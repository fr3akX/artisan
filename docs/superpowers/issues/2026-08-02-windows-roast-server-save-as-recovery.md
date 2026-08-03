# GitHub issue draft: Harden Windows Roast Server Save As recovery

## Title

Harden Windows Roast Server Save As recovery after partial native file operations

## Body

The Artisan Roast Server connector uses a transactional Save As path to keep a server-sourced profile read-only while creating a normal local profile. Two rare Windows native failure sequences remain accepted tradeoffs in the initial connector release:

1. `ReplaceFileW` can move the prior destination into the generated backup and install or move the replacement before returning failure. If immediate restoration also fails, cleanup can leave the destination absent and the original file retained only under the generated backup name. The current retry path may still assume an existing destination and fail to restore that backup directly.
2. Setting delete disposition on the transaction backup can become irreversible before handle close or subsequent absence observation reports an error. Commit can then report a rollback-capable failure even though the backup may already be unavailable.

These cases require filesystem or handle failures during an already failing recovery path. They are not known to affect ordinary Save As operations, POSIX publication, immutable Roast Server cache files, credentials, or tenant isolation.

## Acceptance criteria

- Preserve structured ownership state for every observable partial `ReplaceFileW` outcome.
- If the destination is absent and the retained backup is authoritative, restore the backup directly without requiring an existing destination.
- Never delete a replacement until reconciliation has either succeeded or retained a retryable state.
- Return a structured native-unlink outcome or latch the irreversible delete-disposition point.
- Never report an operation as rollback-capable after the rollback backup has become irreversibly unavailable.
- Preserve concurrent and unrelated destination entries.
- Add native Windows tests for:
  - partial `ReplaceFileW` failure followed by transient restoration failure;
  - successful retry from an absent destination and retained backup;
  - successful delete disposition followed by handle-close failure;
  - inaccessible or delete-pending backup observation;
  - correct committed-versus-rollback-capable classification.
- Keep the accepted POSIX behavior and ordinary local save behavior unchanged.

## Suggested labels

`windows`, `data-integrity`, `roast-server`, `filesystem`
