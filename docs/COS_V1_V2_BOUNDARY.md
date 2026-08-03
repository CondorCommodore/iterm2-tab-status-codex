# COS capability boundary: V1 core and later experiments

This document scopes the Mac-local Codex implementation of the enduring COS strategist contract.
The normative cross-repository authority model is Workspace
[`C2_SUPERVISOR_CONTRACT.md`](https://github.com/CondorCommodore/workspace/blob/main/docs/fleet-plans/C2_SUPERVISOR_CONTRACT.md).
This repository supplies a bootstrap actuator, watcher, recovery projections, and terminal edge; it
does not define a second strategist, scheduler, work-item database, or durable authority.

## V1: usable and recoverable bootstrap path

V1 is Codex-only and Mac-local. It can keep one bounded lane moving with every Control Room process
stopped, survive a supervisor/provider/API interruption, and resume from coord-api without duplicate
effects. Coord-api/BCA remains authoritative for direction, plan generation, tasks, attempts,
messages, leases, sessions, evidence, and results.

### V1 must do

1. Join coord-api as the canonical Codex principal and show a read-only fleet snapshot before action.
   `cosctl status` reports registered worker states, actionable coord items, wake reasons, active plan
   generation, and a decision digest without mutating state. `cosctl preflight` separately proves the
   manifest, landed plan paths, service registration, and edge health.
2. Consume durable BCA direction and the controlling COS plan generation. Publish digest-bound local
   `program.md` and `current-focus.md` projections containing only durable references, bounded current
   actions, known gates, budget/capability policy, and the next reconciliation deadline. They restore
   context; they never authorize a new effect without coord-api readback.
3. Wake the COS model only for a material direction, decision, refill, recovery, or PR/evidence
   transition. Maintain process heartbeat, model acknowledgement, and action-deadline state
   independently.
4. Acquire and renew the shared `workspace:mikebook:c2-supervisor` actuation lease before reserving a
   worker or changing external state. Loss of its epoch makes the bootstrap path read-only immediately.
5. Select one eligible registered Codex worker, reserve it before delivery, and issue one complete
   bounded assignment with objective, repository/worktree, acceptance tests, stopping condition,
   report destination, authorization limits, plan generation, controller epoch, and idempotency key.
6. Bind the assignment to the verified logical agent, coord session, CLI session, terminal identity,
   worktree, task/attempt, and worker-reservation epoch. Reject self-targeting, stale identity, reused
   TTY identity, duplicate assignment, or any fence mismatch.
7. Record delivery and recipient acknowledgement separately. Transport receipts are unnumbered and a
   successful terminal write is not model receipt, response, task completion, or authority evidence.
8. Reconcile `idle`, `reserved`, `running`, `needs_input`, `blocked`, `stale`, `lost`, and `unknown`.
   Preserve reservations on uncertainty and recover or reassign only through the durable lease rules.
9. For an authorized PR lane, verify exact head, run the repository-configured authoritative CI,
   obtain independent review, invalidate evidence after every head change, repair and repeat, and
   issue MERGE disposition only at the authorized boundary.
10. Enforce COS-owned token/model/provider limits and the recorded strategist capability floor. Worker
    or judge routing may change inside policy; strategist capability may not silently downgrade.
11. Run the one event-gated watchdog while armed. It checks every 60 seconds, performs bounded verified
    pokes, and after two failed acknowledgement windows may resume the same CLI UUID headlessly only
    after old-epoch absence. The resumed turn must obtain a successor epoch, reconcile durable state,
    publish a successor projection and readback, release the epoch, and exit.
12. During coord-api loss, continue bounded health checks but issue no new assignment, merge decision,
    priority change, or terminal action. End every cycle with durable state or a precise blocker and a
    bounded next check.

### V1 acceptance evidence

One deterministic vertical test must prove:

```text
durable operator direction -> COS plan generation -> watcher wake -> reconciled projections
-> fenced bootstrap reserve/dispatch -> bounded worker result -> review/CI/MERGE disposition
-> forced interruption -> safe same-thread resume -> successor guidance, with no duplicate effect
```

The adverse bundle includes stale/superseded direction, projection drift, wrong generation, capability
downgrade, budget exhaustion and approved fallback, identity/lease loss immediately before a byte,
false edge acknowledgement, provider/API outage, stale completion, and failed durable readback.

## V1 experiments: shadow only

These may run against synthetic or explicitly authorized low-risk fixtures, but passing them does not
expand V1 authority:

- BCA Precedence queue ordering, ML suggestions, response-obligation automation, and digest summaries;
- tab injection versus headless **routine-assignment transport** or tmux mirroring (the bounded
  watchdog recovery resume in V1 is not part of this A/B experiment);
- Escape-based Immediate/Flash interruption and input-buffer sensing;
- model-assisted screenshot classification;
- Claude/Codex delivery parity; and
- Control Room shadow projection of the same COS plan.

The sequence remains pure reducer/readback, disposable enrolled-runtime lab, then one separately
authorized real-work canary. BCA Precedence supplies a typed shadow proposal; the COS/actuator policy
must explicitly accept it before delivery behavior changes. Delivery never reads an unaudited ML or
issuer suggestion directly.

## Later primary-actuation work

After V1 and shadow evidence are accepted, later phases may add multi-machine enrolled adapters,
full clearinghouse obligation/retry/DLQ behavior, broader fleet refill, and a Control Room
primary-actuation handoff. Control Room continues to execute the COS plan; the strategist and durable
direction authority do not transfer. Failback to bootstrap obtains a later epoch under the same rule.

No phase may create a parallel COS database, task model, scheduler, authority epoch, or local source of
truth. Deployment, credentials, trading, remote-host mutation, destructive cleanup, and branch-policy
changes remain separately authorized operator boundaries.
