# GitHub issue draft: Guarantee hard Roast Server HTTP deadlines

## Title

Guarantee cancellable total HTTP deadlines and timeout cleanup in the Roast Server connector

## Body

The Roast Server API client applies connect/read timeouts, a 12-second monotonic operation budget, bounded upload/download bodies, and a watchdog that closes the active response and owned Requests session. This bounds normal and slow-drip behavior, but stock Requests/urllib3 does not guarantee that `HTTPAdapter.close()` interrupts every active operation.

Potentially non-cancellable boundaries include:

- DNS resolution;
- a blocked socket transmission;
- response-header waiting;
- an upload blocked after the body wrapper has yielded a chunk.

A hostile or stalled endpoint can therefore exceed the nominal operation budget and Artisan's 15-second worker shutdown wait. There are also two related cleanup races:

1. Deadline expiry can occur after `session.request()` returns but before the response is registered with the watchdog. The local response may then escape explicit close.
2. Download expiry can be detected after final destination validation but outside the rollback scope, leaving complete staged bytes. The caller currently discards that stage, so publication remains prevented.

Server error text remains locally fixed and non-disclosing; response/request sizes, redirects, proxies, TLS verification, namespace isolation, and checksum validation are unaffected.

## Acceptance criteria

- Enforce one monotonic total deadline from request preparation through request transmission, response headers, and complete response consumption.
- Make every blocking transport boundary cancellable on Linux, macOS, and Windows.
- Demonstrate that cancellation completes before the controller's shutdown budget.
- Atomically register each response with deadline ownership or close it in every registration race.
- Roll back or discard download staging for every timeout classification, including expiry during finalization.
- Ensure no credential-bearing request thread, socket, response, timer, or adapter survives client closure.
- Preserve no-redirect, no-proxy, verified-TLS, fixed-error, and single-attempt behavior.
- Add causal tests using production-equivalent transport behavior for:
  - blocked DNS;
  - blocked upload transmission;
  - stalled response headers;
  - slow-drip response bodies;
  - expiry between request return and response registration;
  - expiry after destination validation;
  - bounded shutdown while a request is active.

## Suggested implementation directions

Evaluate a transport with explicit cancellation support or isolate each operation behind a killable boundary. Do not treat `Session.close()` alone as proof that an active Requests call was interrupted.

## Suggested labels

`security`, `networking`, `roast-server`, `reliability`
