# BouncyBot Offline Optimizer v1.4.3 — Windows retry-test robustness

Version 1.4.3 is a narrowly scoped corrective release for the remaining Windows-only test failure reported against v1.4.2. It does not change either optimizer algorithm, Market Replay format-2/format-3 ingestion, ATR candidate generation, replay semantics, candidate scoring, stable-region selection, or report interpretation.

## Reported failure

The v1.4.2 Windows run successfully generated the report, but the integration test failed because it asserted that the publication move hook must be called exactly twice:

```text
assert calls == 2
E       assert 3 == 2
```

The test deliberately injected one transient access-denied error. On the supplied Windows environment, the first subsequent real `os.replace` directory move encountered another legitimate transient denial before succeeding. That is behavior the production retry helper is explicitly designed to tolerate, so the exact-count assertion contradicted the production contract.

## Correction

The retry unit tests now use fully simulated move completion when checking exact backoff sequences. Their expected call counts can no longer be changed by antivirus, indexing, synchronization, or other filesystem activity.

The end-to-end SQLite report-publication test still performs a real atomic directory move. It now verifies:

- the configured transient failures were injected;
- the move was retried within the production attempt limit;
- every attempt used the same completed staging directory;
- every attempt targeted the same final content-addressed report directory;
- the process-wide `os.replace` function was not modified;
- the final report and `index.html` exist.

It deliberately does not require the real Windows move to succeed on one specific attempt. The production backoff is left active in this integration test instead of being replaced with a no-op.

## Runtime behavior

Production publication remains unchanged:

- one complete staging directory is prepared first;
- one atomic `os.replace` directory move publishes it;
- only transient access-denied, busy, sharing-violation, and lock-violation errors are retried;
- publication never falls back to copying individual files into the final directory;
- non-transient failures are raised immediately;
- an existing final destination is never overwritten.
