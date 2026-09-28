# Reconciliation / adoption subsystem - TARGET architecture (2026-09-28)

Proposed target design for lane 5e (reconcile + adopt). Grounded in the verified
findings brief `recon-review-findings` and a fresh read of
`reconciliation_monitor.py`, `safety.py`, `cli.py`, `startup.py`, `session_impl.py`,
`fastapi_app.py`, `engine.py`, `trading_cache.py`.

Key invariants the design keeps:

- `tradex_runtime` may NOT import `tradex_interfaces` (pinned by
  `tests/test_import_boundaries.py`). The join of adoption policy and the OMS
  book belongs in `tradex_interfaces`; the timer belongs in `tradex_runtime`.
- The session owns the monitor lifecycle (start in `start()`, stop in `stop()`
  before broker teardown).
- Adoption is gated by the sealed `VenueRead` (failed/partial/stale refused
  before mutation) and by `unsettled_local_instruments`.

## 1. Architecture: packages, composition root, boot convergence

```mermaid
flowchart TB

  subgraph BOOT["Three boot paths (all pass the factory)"]
    direction TB
    B1["main() -> boot() paper session<br/>cli.py L311, reused by cmd_serve"]
    B2["cmd_serve live/other<br/>cli.py L474, own boot()"]
    B3["serve_app() uvicorn spawn<br/>fastapi_app.py L518-533<br/>workers>1 / --reload, env TRADEX_SERVE_*"]
  end

  subgraph IFACE["tradex_interfaces (upper layer)"]
    direction TB
    CR["COMPOSITION ROOT<br/>cli.build_reconciliation_monitor<br/>cli.py L389 - sees both package halves"]
    SEAMS["build_reconciliation_monitor_seams<br/>safety.py L1395<br/>closure seam: reconcile / adopt"]
    SAFETY["Adoption policy<br/>read_venue_positions / VenueRead<br/>derive_venue_adoption / VenueAdoption<br/>unsettled_local_instruments<br/>adopt_venue_positions"]
    SROUTE["Safety surface<br/>GET /api/charts/safety<br/>build_safety_report / _rerun_reconciliation<br/>dry-run adopt=False"]
  end

  subgraph RUNTIME["tradex_runtime (lower layer - no imports from interfaces)"]
    direction TB
    BOOTFN["boot() / _boot_tail<br/>startup.py<br/>invokes factory BEFORE session.start()"]
    MON["ReconciliationMonitor<br/>reconciliation_monitor.py<br/>30s daemon thread, seams = plain callables"]
    SESS["TradingSession<br/>session_impl.py<br/>OWNS monitor: start() L212 / stop() L251"]
    STARTUPRECON["live-only startup + post-reconnect recon<br/>_run_startup_reconciliation L1348<br/>engine.reconcile(broker_orders=, broker_positions=) CORRECT"]
  end

  subgraph LOWER["tradex_execution / tradex_domain (lower layer)"]
    direction TB
    ENG["ExecutionEngine.reconcile<br/>engine.py L1351<br/>pure compare + ReconciliationResult event"]
    CACHE["TradingCache<br/>set_position / all_positions<br/>per-instrument position_locks"]
  end

  VENUE[("Venue / broker<br/>get_positions / get_orderbook")]

  ASGI["ASGI lifespan _lifespan<br/>uvicorn.run never returns on SIGTERM<br/>teardown lives here"]

  B1 -->|"reconciliation_monitor_factory=..."| BOOTFN
  B2 -->|"reconciliation_monitor_factory=..."| BOOTFN
  B3 -->|"serve_app: same factory"| BOOTFN

  BOOTFN -->|"factory(broker, engine.cache, engine, metrics)"| CR
  CR -->|"builds"| SEAMS
  CR -->|"ReconciliationMonitor(reconcile=, adopt=)"| MON
  BOOTFN -->|"reconciliation_monitor=..."| SESS
  BOOTFN -->|"session.start()"| SESS
  SESS -->|"starts/stops"| MON

  SEAMS -->|"reconcile seam<br/>DEFECT: calls engine.reconcile() no-args"| ENG
  SEAMS -->|"adopt seam: read, guard, derive, apply"| SAFETY
  SAFETY -->|"read_venue_positions (sealed)"| VENUE
  SAFETY -->|"apply: cache.set_position per row"| CACHE
  SROUTE -->|"dry-run / re-run, same functions"| SAFETY
  SROUTE -->|"reconcile WITH args (correct reference)"| ENG
  STARTUPRECON --> ENG
  STARTUPRECON --> VENUE
  ENG --> CACHE
  ENG -->|"ReconciliationResult per call<br/>event store (Finding 4)"| EVENT[("Event store")]
  ASGI -->|"_stop_bound_session"| SESS

  style CR fill:#fff3cd,stroke:#b8860b
  style MON fill:#d1ecf1,stroke:#0c5460
  style SESS fill:#d1ecf1,stroke:#0c5460
  style SAFETY fill:#d4edda,stroke:#155724
  style ASGI fill:#f8d7da,stroke:#721c24
```

Composition root stays in `tradex_interfaces.cli` (the only module that can see
both halves). The factory seam is the *only* thing crossing the package
boundary downward: `boot()` receives `reconciliation_monitor_factory` and calls
it inside `_boot_tail` **before** `session.start()` (startup.py L989-994), so a
monitor assigned post-hoc would never be started. The session owns the monitor
from that point; the monitor's seams are plain closures, so `tradex_runtime`
never sees an `interfaces` type.

## 2. Component diagram: the reconcile/adopt subsystem

```mermaid
flowchart LR

  subgraph MONBOX["ReconciliationMonitor (runtime)"]
    POLL["poll_once: _reconcile() then _adopt()<br/>never raises, 30s interval<br/>last_drifts / last_adoption"]
  end

  SEAMR["seams._reconcile()<br/>closure, safety.py L1470"]
  SEAMA["seams._adopt()<br/>closure, safety.py L1441"]

  subgraph SAFETYBOX["safety.py (interfaces)"]
    ADOPTFN["adopt_venue_positions<br/>sealed read -> refusal gate -> derive -> apply"]
    READFN["read_venue_positions -> VenueRead<br/>failed / partial / complete (sealed)"]
    DERIVE["derive_venue_adoption -> VenueAdoption<br/>pure: positions / retained / cleared / skipped / previous"]
    UNSET["unsettled_local_instruments<br/>NEW/PENDING/ACK/SUBMITTED/PARTIALLY_FILLED/UNKNOWN"]
    APPLY["_apply_venue_adoption<br/>cache.set_position loop<br/>Finding 6: per-row, not batch-atomic"]
  end

  ENGINE["ExecutionEngine.reconcile<br/>broker_orders= / broker_positions= / local_positions=<br/>+ appends ReconciliationResult (Finding 4)"]
  CACHE["TradingCache<br/>set_position / all_positions / all_orders<br/>_positions_lock + per-instrument position_lock"]
  STORE[("Event store<br/>ReconciliationResult, order stream")]
  SAFESURF["Safety surface<br/>build_safety_report / _rerun_reconciliation<br/>GET /api/charts/safety"]
  METRICS["MetricsRegistry<br/>venue_adoption_* / venue_read_failed_total<br/>reconciliation_drift_total.severity.kind"]
  VENUE[("Venue broker<br/>get_positions")]

  POLL --> SEAMR
  POLL --> SEAMA
  SEAMR -->|"DEFECT: engine.reconcile() with no args<br/>-> both compare blocks skipped -> []"| ENGINE
  SEAMA -->|"known_instruments from cache<br/>unsettled from cache.all_orders"| ADOPTFN
  ADOPTFN --> READFN
  READFN --> VENUE
  ADOPTFN -->|"refused: failed/partial/stale"| METRICS
  ADOPTFN --> DERIVE
  DERIVE -.->|"Finding 2: unsettled guard ONLY on both-sides branch;<br/>venue-only branch unguarded"| UNSET
  DERIVE --> APPLY
  APPLY --> CACHE
  ENGINE --> CACHE
  ENGINE --> STORE
  SAFESURF -->|"last_reconciliation or _rerun_reconciliation<br/>passes broker_positions/local_positions (correct)"| ENGINE
  SAFESURF -->|"dry-run adopt=False: same derive, no apply"| ADOPTFN
  POLL --> METRICS

  style SEAMR fill:#f8d7da,stroke:#721c24
  style DERIVE fill:#fff3cd,stroke:#b8860b
  style APPLY fill:#fff3cd,stroke:#b8860b
```

## 3. Sequence: one 30s poll cycle (with the two confirmed defects)

```mermaid
sequenceDiagram
  autonumber
  participant T as Monitor thread (30s)
  participant SR as seams._reconcile
  participant E as ExecutionEngine.reconcile
  participant SA as seams._adopt
  participant V as Venue broker
  participant D as derive_venue_adoption
  participant C as TradingCache
  participant ES as Event store

  T->>SR: poll cycle N
  SR->>E: engine.reconcile()  [NO ARGUMENTS]

  rect rgb(255, 235, 235)
    Note over SR,E: CONFIRMED DEFECT - drift-reporting claim broken
    E-->>E: broker_positions is None -> skip position compare
    E-->>E: broker_orders is None -> skip order compare
    E->>ES: append ReconciliationResult(drift_count=0)  (Finding 4 noise)
    E-->>SR: [] (ALWAYS)
    Note over SR: last_drifts permanently empty.<br/>Contrast _rerun_reconciliation which passes<br/>broker_positions/local_positions correctly.
  end

  T->>SA: _adopt()
  SA->>C: all_positions() -> local_positions, all_orders() -> unsettled set
  Note over SA: Finding 5: _unsettled() returns frozenset() on read error<br/>(fail-OPEN guard). known_instruments recomputed twice per row.
  SA->>V: read_venue_positions(known_instruments)
  alt read failed / partial / stale (last_read_at)
    V-->>SA: VenueRead.refused
    SA-->>T: AdoptionOutcome(applied=False, refusal=...)
    Note over T: venue_read_failed_total / venue_adoption_refused_total
  else read complete
    V-->>SA: VenueRead(positions, read_at) sealed
    SA->>D: derive_venue_adoption(local, venue, unsettled)
    Note over D: both-sides branch: unsettled guard applied (skip_unsettled_fill)

    rect rgb(255, 235, 235)
      Note over D: FINDING 2 - venue-only branch unguarded
      D->>D: iid in venue - local: NO unsettled check
      Note over D: A venue-only row for an instrument with an<br/>in-flight LOCAL order can be adopted over the<br/>unsettled fill (venue snapshot predates it).
    end

    D-->>SA: VenueAdoption(positions, cleared, skipped, previous)
    SA->>C: _apply_venue_adoption: set_position per row

    rect rgb(255, 249, 235)
      Note over SA,C: FINDING 6 - apply is per-row under _positions_lock only
      Note over SA,C: A fill (PositionAccountant under per-instrument lock)<br/>can interleave mid-apply: transient book inconsistent.<br/>Terminal state converges (adoption idempotent).
    end

    SA-->>T: AdoptionOutcome(applied=True)
  end
  Note over T: venue_adoption_{adopted,cleared,skipped}_total,<br/>venue_adoption_last_adopted
```

## 4. Lifecycle: monitor, session ordering, ASGI teardown, spawn paths

```mermaid
stateDiagram-v2
  direction TB

  state "Monitor lifecycle" as MON {
    [*] --> Constructed: build_reconciliation_monitor<br/>composition root, BEFORE session.start()
    Constructed --> Running: session.start() -> monitor.start()
    Running --> Running: poll every 30s (poll_once never raises)
    Running --> ThreadDead: unhandled seam error
    ThreadDead --> Running: start() again (guard is is_alive,<br/>restartable after death)
    Running --> Stopped: session.stop() -> monitor.stop()<br/>join(5s), BEFORE broker teardown
    Stopped --> Running: start() (restartable)
    Stopped --> [*]
    Constructed --> [*]: session never started (stop() no-ops on NEW)
  }

  state "Session start ordering (boot)" as SORDER {
    [*] --> Building: _boot_tail builds services
    Building --> MonitorBuilt: factory(broker, engine.cache, engine, metrics)<br/>startup.py L989
    MonitorBuilt --> StartupRecon: live only: _run_startup_reconciliation<br/>BEFORE session.start() (C2: no READY over critical drift)
    StartupRecon --> Started: session.start()<br/>feed_monitor.start() then reconciliation_monitor.start()
    Started --> [*]
  }

  state "Session stop ordering" as TORDER {
    [*] --> StopSignalled
    StopSignalled --> MonitorsStopped: feed_monitor.stop() then<br/>reconciliation_monitor.stop()
    MonitorsStopped --> BrokerClosed: market feed, MTM, bus,<br/>fill bridge, stream, master scheduler, broker.close()
    BrokerClosed --> [*]
  }

  state "Process / spawn paths" as SPAWN {
    state "path 1: main()->boot() paper, cmd_serve reuses session" as P1
    state "path 2: cmd_serve live/other, own boot()" as P2
    state "path 3: serve_app() uvicorn workers>1/--reload spawn<br/>env TRADEX_SERVE_*; live refused with workers/reload" as P3
    [*] --> P1
    [*] --> P2
    [*] --> P3
    P1 --> LIFE
    P2 --> LIFE
    P3 --> LIFE
  }

  state "ASGI lifespan teardown" as LIFE {
    [*] --> Serving: yield
    Serving --> Teardown: finally: _stop_bound_session(app.state.session)
    Note right of Teardown: uvicorn.run never returns on SIGTERM<br/>(capture_signals re-raises, disposition SIG_DFL).<br/>Caller-side finally is unreachable;<br/>lifespan shutdown runs before the re-raise,<br/>covering workers/reload child sessions too.
    Teardown --> [*]: then session.stop() (TORDER)
  }

  Started --> StopSignalled: stop() / lifespan / cmd_serve finally
```

## Design changes proposed

**1. Fix the reconcile seam (confirmed defect).** `seams._reconcile` currently
calls `engine.reconcile()` with no arguments, which skips both compare blocks and
always returns `[]` - the "reports drift exactly as before" claim is false and
`last_drifts` is permanently empty. Change the closure to mirror
`_rerun_reconciliation` (safety.py L603): read `engine.cache.all_positions()` and
`broker.get_positions()` once, then call
`engine.reconcile(broker_positions=venue_positions, local_positions=positions)`.
Even better: have the adopt seam share that single venue read (read once per poll,
derive both drift and adoption from it) so the reconcile and adopt halves cannot
disagree and the venue's rate-limited endpoint is hit once per 30s, not twice.
Orders drift (`compare_orders`) stays out of the seam or gets the same treatment
via `broker.get_orderbook()` - decide explicitly, since startup reconciliation
does compare orders.

**2. Apply the unsettled guard to the venue-only branch (Finding 2).** In
`derive_venue_adoption`, the loop over `set(venue_by_id) - set(local_by_id)`
never consults `unsettled_instruments`, so a venue-only row for an instrument
with an in-flight local order can be adopted over the unsettled fill. The fix is
symmetric and cheap: before `_adopted_position(None, venue)`, skip with
`SKIP_UNSETTLED_FILL` when `iid in unsettled`, exactly as the both-sides branch
does at L936. This makes the docstring's "An unsettled fill is never adopted
over" true on every branch. Also close the Finding 5 fail-open: when
`_unsettled()` cannot read orders, the guard should fail CLOSED (treat all
instruments as unsettled, or refuse the adoption with a distinct refusal code)
rather than silently returning `frozenset()` and adopting unguarded.

**3. Batch-atomic adoption apply - yes, under one lock (Finding 6).**
`_apply_venue_adoption` writes `positions + cleared` one `set_position` at a
time, each under only `_positions_lock` for that single call; a fill writing
under a per-instrument `position_lock` can interleave mid-apply, so the
transient book mixes pre- and post-adoption rows (strategies reading between
writes see an inconsistent book). Terminal state converges, but the transient is
observable. Proposal: give `TradingCache` a single batch write,
`set_positions(Sequence[Position])`, that takes `_positions_lock` once and swaps
the whole row set under it (and, where per-instrument RMW matters, acquires the
per-instrument locks in sorted-key order to stay deadlock-free against the fill
path). The plan is already a complete, pure `VenueAdoption` - the apply should
match its all-or-nothing semantics. Keep it best-effort-free: if the cache lacks
the batch method, keep today's per-row loop as a fallback but log it as
non-atomic.

Adjacent (cheap, in scope of the same edit): Finding 4 - once the seam is fixed,
`engine.reconcile` appends `ReconciliationResult` every 30s forever. Keep the
append but only on drift (or drop the count when zero) so the system stream does
not grow one row per half-minute. Finding 3 - delete dead `_cleared_position`
(superseded by the inline `_flat_position` fallback).
