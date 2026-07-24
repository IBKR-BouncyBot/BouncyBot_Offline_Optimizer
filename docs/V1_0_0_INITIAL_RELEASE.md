# BouncyBot Offline Optimizer v1.0.0

Version 1.0.0 establishes a separate, non-trading application for retrospective analysis of BouncyBot's existing SQLite and market-capture evidence.

The release intentionally does not modify the trading bot, request new market data, connect to IBKR, or write optimized settings back into the production database. Its output is an atomically published, checksummed report package and a bounded list of paper-trading experiments.

Version 1.0.0 distinguishes normal and protective exits, reconstructs alternate ATR windows only from candidate-specific saved history, and labels every result according to data coverage and sample strength. It does not claim to infer globally optimal settings from fill-centred captures.
