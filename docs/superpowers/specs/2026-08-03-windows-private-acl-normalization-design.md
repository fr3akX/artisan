# Windows Private ACL Normalization Design

## Problem

The Roast Server connector hardens generated directories and files with a protected Windows DACL containing exactly one full-control ACE for the current user. The SDDL currently gives that ACE object- and container-inheritance flags for every filesystem object, and verification requires those flags unconditionally.

Windows removes inheritance flags when the DACL is applied to a regular file. The resulting ACL remains private and is reported by `icacls` as one current-user `(F)` entry, but connector verification rejects it. `Outbox.open()` therefore stops after creating a zero-byte `.outbox.lock`; it never creates `outbox.sqlite3` or authenticates the persisted Roast Server credential on restart.

## Security invariant

The fix must not weaken private-storage verification. Every connector object must retain:

- a protected DACL;
- exactly one allow ACE;
- the current process user's SID;
- full-control rights;
- no inherited, additional, malformed, or trailing ACE data;
- the expected readonly state for immutable files.

Directories additionally require object- and container-inheritance flags so descendants inherit the private ACL. Regular files require no inheritance flags, matching Windows normalization.

## Design

`_WindowsNativeLayer` will determine whether the final handle references a directory using its existing attribute query.

When constructing the security descriptor:

- directories use `D:P(A;OICI;FA;;;<sid>)`;
- regular files use `D:P(A;;FA;;;<sid>)`.

When verifying the DACL, the expected ACE flags will be passed explicitly:

- directories: `OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE`;
- regular files: `0`.

All existing exact ACE type, size, SID, access-mask, DACL-protection, and readonly checks remain unchanged.

## Testing

Portable Win32 ABI tests will cover acceptance of exact directory and regular-file ACE flags and rejection of mismatched or unexpected flags. Native Windows tests will harden and verify both a directory and a regular file, and the existing native outbox-open tests will continue to prove that SQLite initialization works with the hardened lock.

Linux/macOS behavior and POSIX permission checks are unchanged.

## Delivery

The implementation will be committed to `ci/appveyor-unsigned-pr` / PR #2. AppVeyor's PR path builds unsigned artifacts, allowing physical validation on Windows. The signed-build restoration remains tracked in issue #3.
