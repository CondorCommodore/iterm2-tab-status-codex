# COS capability boundary: V1 core and later experiments

This document scopes the Mac-local terminal-edge implementation of one principal-neutral COS control
contract. Codex and Claude are the mandatory initial adapters. That supported-runtime set grants no
authority: authority still derives from authenticated direction, capability, and the live lease epoch.
The normative cross-repository authority model is Workspace
[`C2_SUPERVISOR_CONTRACT.md`](https://github.com/CondorCommodore/workspace/blob/main/docs/fleet-plans/C2_SUPERVISOR_CONTRACT.md).
This repository supplies a bootstrap actuator, watcher, recovery projections, and terminal edge; it
does not define a second strategist, scheduler, work-item database, or durable authority.

## V1: usable and recoverable bootstrap path

V1 applies one runtime-neutral control contract on this Mac, with Codex and Claude as the mandatory
initial conformance set. Its acceptance target is to keep one bounded lane moving with every Control
Room process stopped, survive a supervisor/provider/API interruption, and resume from coord-api
without duplicate effects. Coord-api/BCA remains authoritative for direction, plan generation,
tasks, attempts, messages, leases, sessions, evidence, and results. Principal and runtime names are
registered coordinates, not authority-bearing branches.

**Implementation disclosure (2026-08-03):** the terminal edge enforces the shared lease for bounded
terminal delivery. The projection-content validator now enforces manifest-bound current-actions and
program projection content, and the durable-vs-world external-state sweep now persists typed
divergence findings and suppresses continuation on unattributed external mutation. The full vertical
acceptance target below has still not been proven. Documentation, fixtures, or a successful
edge-only test must not be reported as full V1 acceptance.

The numbered requirements below are **target acceptance gates, not shipped-property claims**. A
requirement named as unimplemented makes V1 unaccepted until its implementation and adverse evidence
land; it does not weaken, waive, or make the requirement optional.

### V1 must do

1. Join coord-api as the exact registered principal/runtime and show a read-only fleet snapshot before action.
   `cosctl status` reports registered worker states, actionable coord items, wake reasons, active plan
   generation, and a decision digest without mutating state. `cosctl preflight` separately proves the
   manifest, landed plan paths, service registration, and edge health.
2. Consume durable BCA direction and the controlling COS plan generation. Publish digest-bound local
   `program.md` and `current-focus.md` projections containing only durable references, bounded current
   actions, known gates, budget/capability policy, and the next reconciliation deadline. They restore
   context; they never authorize a new effect without coord-api readback.
   *Enforcement honesty (2026-08-03):* `program.md`, `current-focus.md`, and `current-actions.txt`
   now reject out-of-bound manifest references and header/body drift at readback. What remains
   unproven is the full runtime path from durable direction through recovery, interruption, resume,
   and merge evidence—not the local projection-content reader itself.
3. Wake the COS model only for a material direction, decision, refill, recovery, or PR/evidence
   transition. Maintain process heartbeat, model acknowledgement, and action-deadline state
   independently.
4. Acquire and renew the shared `workspace:mikebook:c2-supervisor` actuation lease before reserving a
   worker or changing external state. Loss of its epoch makes the bootstrap path read-only immediately.
5. Select one eligible registered Codex or Claude worker through the shared adapter contract, reserve
   it before delivery, and issue one complete bounded assignment with objective,
   repository/worktree, acceptance tests, stopping condition, report destination, authorization
   limits, plan generation, controller epoch, and idempotency key.
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
    pokes, and after two failed acknowledgement windows may resume the same CLI/thread identity
    headlessly through the registered adapter only after old-epoch absence. The initial adapter
    mechanics are `codex exec resume <session-id>` and `claude --resume <session-id> --print`; those
    commands confer no authority and live only behind the shared resume operation. The resumed turn
    must obtain a successor epoch, reconcile durable state, publish a successor projection and
    readback, release the epoch, and exit.
12. During coord-api loss, continue bounded health checks but issue no new assignment, merge decision,
    priority change, or terminal action. End every cycle with durable state or a precise blocker and a
    bounded next check.

### V1 acceptance evidence

A single-runtime test cannot falsify a provider-name or principal-name special case. The same
deterministic vertical test must therefore pass twice: Codex COS -> Claude worker -> distinct Codex
reviewer, then Claude COS -> Codex worker -> distinct Claude reviewer. All principals in each run are
distinct, assignments are sequential, and neither runtime receives special authority. Each run must
prove:

```text
durable operator direction -> COS plan generation -> watcher wake -> reconciled projections
-> fenced bootstrap reserve/dispatch -> bounded worker result -> review/CI/MERGE disposition
-> forced interruption -> safe same-thread resume -> successor guidance, with no duplicate effect
```

The adverse bundle includes stale/superseded direction, projection drift, wrong generation, capability
downgrade, budget exhaustion and approved fallback, identity/lease loss immediately before a byte,
false edge acknowledgement, provider/API outage, stale completion, and failed durable readback.

The adverse bundle must also include **durable-vs-world divergence**: mutate GitHub state behind the
supervisor's back (close a tracked PR, delete a tracked branch) with no coord-api record, then force
recovery. A pass requires the recovered supervisor's external-state sweep to surface the mutation as
an unattributed actuation (typed finding, escalated), not to reconcile cleanly against coord-api and
proceed as if the work still exists. This is the observed 2026-08-02 failure class: unfenced GitHub
mutations left no coord-api trace, and coord-api-only reconciliation cannot detect that by
construction. The current implementation persists typed findings and blocks continuation wakeups on
that divergence class, but the full adverse-bundle acceptance run named above remains outstanding.
(Normative statement: workspace `C2_SUPERVISOR_CONTRACT.md` §2.)

## V1 experiments: shadow only

These may run against synthetic or explicitly authorized low-risk fixtures, but passing them does not
expand V1 authority:

- BCA Precedence queue ordering, ML suggestions, response-obligation automation, and digest summaries;
- tab injection versus headless **routine-assignment transport** or tmux mirroring (the bounded
  watchdog recovery resume in V1 is not part of this A/B experiment);
- Escape-based Immediate/Flash interruption and input-buffer sensing;
- model-assisted screenshot classification;
- adapters beyond the required Codex/Claude conformance set; and
- Control Room shadow projection of the same COS plan.

An additional adapter is unaccepted until it passes the same contract; it does not receive lesser or
greater authority because of its runtime name.

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
