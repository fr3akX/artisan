# Inventory Roast Title at CHARGE Design

**Status:** Approved 2026-08-20

## Goal

When a roast successfully reaches CHARGE with a selected Roast Server inventory item, derive the roast title from the selected inventory item name and the local CHARGE date and time.

## Behavior

- Run only after inventory reservation and CHARGE marking have succeeded.
- Generate the title as `Inventory item name – YYYY-MM-DD HH:mm`.
- Use the computer's local time at successful CHARGE.
- Replace the translated default title `Roaster Scope`, or an empty title, automatically.
- If the current title is non-default, ask whether to replace it.
- The replacement confirmation uses Yes/No and defaults to No.
- Declining replacement preserves the existing title and does not cancel or undo CHARGE.
- A failed or cancelled CHARGE, or CHARGE without a selected inventory item, does not prompt and does not change the title.
- Repeating CHARGE after undo treats the previously generated title as non-default and asks before replacing it.

## Architecture

`ApplicationWindow` owns a focused UI helper that reads the current inventory lot name and roast title, formats the candidate title, asks for confirmation when necessary, and mutates only `qmc.title`. `tgraphcanvas._markCharge()` calls that helper after releasing `profileDataSemaphore` and only when CHARGE was marked successfully, before its normal redraw so the new title appears in that redraw.

This keeps dialogs outside the profile semaphore, preserves the existing inventory reservation transaction, and avoids changing titles on failed CHARGE attempts.

## Testing

Focused unit tests cover default and empty automatic replacement, custom-title confirmation acceptance and rejection, missing inventory, and the canvas integration boundary proving the helper runs only after successful CHARGE and after semaphore release.

The same PR also removes temporary pytest diagnostics and assigns concise IDs to the two large-byte outbox parametrizations that caused verbose macOS collection to emit approximately 16 MiB node IDs and stall GitHub Actions.
