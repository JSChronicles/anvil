# Provider Runtime And Session Lifecycle

Use this reference when creating or reviewing an Anvil provider, provider
plugin, runtime adapter, session factory, or SDK client cache.

## Ownership Boundaries

Keep ownership explicit:

- A `Provider` prepares provider-owned execution targets and creates one
  `ProviderExecutionRuntime` per active execution target.
- A runtime's `build_session(region=...)` returns the task-facing session for
  one concrete execution-target/location coordinate.
- The engine shares that session across every task planned for the coordinate.
  It removes the session from its cache after the coordinate's last task
  settles and calls `close()` when the session exposes it.
- `ProviderExecutionRuntime.close()` is the fallback for resources owned by the
  runtime itself and must handle cancellation, partial construction, and
  failures.

Do not retain the same heavyweight session or client in both the returned
session and a runtime list or cache. If a provider must retain it, ensure the
coordinate session's `close()` releases the underlying SDK resources and clears
the retained reference, or provide an equivalent bounded lifecycle.

## Cleanup Contract

A task-facing session that owns any of the following should implement an
idempotent `close()` method:

- HTTP transports or connection pools;
- generated SDK clients;
- background workers or executors;
- refreshable credentials with external resources;
- per-coordinate caches containing provider response objects.

Close every owned resource even when one close operation fails. Clear caches
before raising cleanup errors so a failed close does not keep the entire object
graph reachable. Redact credentials and tokens from cleanup errors.

Runtime `close()` must release all remaining provider-owned resources and be
safe after zero, partial, or complete session construction. Do not rely on
garbage collection for network clients.

## Cache Rules

- Bound cache lifetime to the narrowest valid ownership scope: invocation,
  coordinate, execution target, or configured target.
- Add explicit eviction when the key space grows with accounts, subscriptions,
  projects, repositories, regions, or locations.
- Avoid process-wide SDK-client caches unless their maximum key space is both
  small and documented.
- A concurrency limit bounds active work; it does not bound a cache that retains
  every completed key.
- Cache compact authentication or discovery facts instead of complete SDK
  clients when either representation can satisfy later work.

## Provider Review Checklist

1. Identify who owns each credential, session, SDK client, transport, and cache.
2. Verify `build_session()` does not duplicate ownership in the runtime.
3. Verify coordinate settlement closes and dereferences heavyweight sessions.
4. Verify runtime cleanup handles cancellation and exceptions.
5. Verify discovery and session-factory caches have bounded key spaces or
   explicit eviction.
6. Test that tasks in one coordinate reuse one session.
7. Test that the session closes after the coordinate's last task and before a
   broader fan-in task runs.
8. Test that every remaining resource is closed after a task, outcome hook, or
   cleanup failure.

For large-fleet tests, assert lifecycle events rather than relying only on peak
RSS measurements. A useful regression proves that completed coordinates are no
longer strongly reachable while later targets are still executing.
