# BouncyBot Offline Optimizer v2.2.0

## Exact refinement acceleration

Version 2.2.0 accelerates Stage 3 refinement and outward search-boundary probes without reducing the candidate grid or changing any analytical formula.

For each ATR window, multiplier, clamp pair, and zero-trail mode, the optimizer prepares read-only arrays containing the exact effective percentage and clamp state produced after multiplication, clamping, and two-decimal rounding. Values are stored as integer hundredths, so the prepared representation is exact for the values used by BouncyBot.

Profiles are collapsed only when the complete effective arrays for initial drop, BUY rebound, minimum profit, SELL trail, and ATR-adaptive protective SELL behavior are identical across every prepared session. Manual and disabled protective policies remain distinct. One representative is replayed, after which every nominal profile is restored before ranking, stable-region construction, Pareto analysis, boundary extension, bootstrap, leave-one-day-out, walk-forward validation, and report generation.

Large recordings use smaller worker batches to improve tail latency and progress responsiveness. Automatic process selection can use up to sixteen workers when CPU count and available memory permit, while preserving manual 1–64 worker selection.
