# BouncyBot Offline Optimizer v2.1.1

## Pyright ATR-provider typing correction

Version 2.1.1 corrects four Pyright 1.1.411 errors in the exact compact and
process-parallel replay engine introduced by v2.1.0.

The replay core historically annotated ATR vectors as
`Sequence[float | None]`. The reference engine supplies ordinary Python lists,
but the compact engine supplies read-only NumPy arrays and memory maps. NumPy
arrays provide the exact operations the replay state machine uses—`len()` and
integer indexing—but NumPy's static type is not declared as a Python
`Sequence`. Pyright therefore rejected the compact callback even though the
runtime behavior was valid.

The replay core now defines one minimal read-only protocol containing only:

```text
length
integer indexing returning an ATR value or missing value
```

Both `list[float | None]` and `numpy.ndarray[float64]` satisfy that protocol.
The core remains independent of NumPy and retains meaningful static checking;
the callback was not weakened to `Any`.

Two reference ATR-provider closures also accepted a concrete
`list[IbrecTick]` even though the replay core promises callers only a
`Sequence[Any]`. Callback parameter types are contravariant, so Pyright
correctly rejected those narrower closures. Their annotations now accept the
same sequence contract as the core. The actual reference inputs remain the
same lists as before.

This release does not change:

- ATR reconstruction or values
- compact tick or ATR storage
- profile evaluation order
- multiprocessing behavior
- Stage 3 or boundary-probe search
- replay state transitions
- scoring, robustness gates, or recommendation selection
- deterministic report identity or report content for the same analytical
  contract

The source archive is also rebuilt without the local Windows virtual
environment and generated caches that were inadvertently present in the
submitted v2.1.0 source ZIP.
