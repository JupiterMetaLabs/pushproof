# Review checklist

Work top-down; stop expanding a category once it is clearly irrelevant to the diff.

## Cross-file interactions (check first)
- Guards split across layers: route/middleware allow-lists vs handler checks. Does any path
  now reach a handler with an assumption (authenticated, validated, non-null) no longer true?
- Changed function signatures, return types, error semantics or units: are all callers updated?
- Config/feature flags/env defaults changed in one file and consumed in another.
- Serialization contracts: struct/JSON/proto/ABI field changes vs. readers and writers.
- Migrations vs. code that reads the new or old schema; ordering during deploy.

## Correctness
- Inverted or weakened conditions (`and`/`or`, `is not None and ...`, off-by-one, `<` vs `<=`).
- Null/None/empty handling, default values, integer overflow, precision and rounding (money,
  token decimals, wei/gwei).
- Error paths: swallowed exceptions, ignored return values, partial writes without rollback.
- Resource handling: leaks, missing close/defer, unbounded growth.

## Concurrency and ordering
- Shared mutable state, locks, check-then-act races, goroutine/thread lifetimes.
- Nonce, sequence or ordering assumptions; idempotency and retries; replay.

## Security
- AuthN/AuthZ bypass, missing ownership checks, privilege escalation.
- Injection (SQL, shell, path traversal, template), unsafe deserialization, SSRF.
- Secrets, keys or tokens committed, logged or returned to clients.
- Crypto misuse: weak randomness, signature/verification gaps, missing chain-id/domain separation.

## Blockchain and smart contracts (when relevant)
- Reentrancy, checks-effects-interactions, access modifiers, `tx.origin`.
- Unchecked external calls, approval/allowance logic, decimals, fee-on-transfer tokens.
- Upgradeability storage layout, initializer protection.
- Relayer/indexer assumptions about finality, reorgs and event ordering.

## Tests
- Does the change have tests for the new behaviour and the failure path?
- Do existing tests still assert the intended behaviour, or were they edited to pass?

Report a test gap only when the untested path carries real risk; tie it to a concrete
scenario.
