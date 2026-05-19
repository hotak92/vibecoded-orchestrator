---
title: Saga Pattern for Distributed Workflows
type: pattern
tags:
  - patterns
  - workflow
  - integration
  - reliability
  - mid-level-architecture
  - microservices
  - automation
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
status: active
---

# Saga Pattern for Distributed Workflows

When a workflow touches multiple systems (charge payment → reserve inventory → schedule shipment → send confirmation), you cannot wrap it in a database transaction. Each system has its own state and its own failure modes. The **saga pattern** is how you achieve "all steps succeed or all compensate" semantics across this distributed mess.

## The core idea

A saga is a sequence of local transactions, each with a defined **compensation** that reverses its effect. If step N fails, run compensations for steps 1..N-1 in reverse order.

```
Forward path:    A → B → C → D
Failure at C:    A → B → C(fail) → compensate(B) → compensate(A) → end
```

Compensations are NOT rollbacks — the original steps already committed. Compensations are forward operations that semantically undo the effect (refund the payment, release the reservation, cancel the shipment).

## Two orchestration styles

### Choreography (event-driven, no central coordinator)

Each service emits events; downstream services react. Compensations are emitted as `*.failed` events that upstream services subscribe to.

```
OrderService → "order.created" → InventoryService
InventoryService → "inventory.reserved" → PaymentService
PaymentService → "payment.failed" → (event)
                                      ├─ InventoryService → reverse reservation
                                      └─ OrderService → mark cancelled
```

**Pros**: services stay decoupled; no single point of failure.
**Cons**: hard to reason about ("what's the current state of this saga?"); failure-handling logic scattered across services; debugging requires log correlation across many services.

**When**: small number of steps (<5), team is comfortable with event-driven debugging, services already have a robust event bus.

### Orchestration (central coordinator drives the flow)

A workflow engine or dedicated orchestrator service calls each step in order, handles failures, and triggers compensations.

```
SagaOrchestrator:
  try:
    reservation_id = inventory.reserve(...)
    charge_id = payment.charge(...)
    shipment_id = shipping.schedule(...)
    notification.send(...)
  except step_failure:
    if shipment_id: shipping.cancel(shipment_id)
    if charge_id: payment.refund(charge_id)
    if reservation_id: inventory.release(reservation_id)
    raise
```

**Pros**: linear code; easy to reason about; central place for retry / timeout / observability.
**Cons**: orchestrator becomes coupling point; if implemented naively (in-memory), a crash mid-saga corrupts state.

**When**: most production workflows. Use a durable execution engine (Temporal, Inngest, AWS Step Functions, Camunda) to get the "linear code" benefits without the "crash = corrupt" downside. See `[[relatedTo::Workflow Engine Tradeoffs 2026]]`.

## Compensation design rules

1. **Every step must have a compensation, including the "do nothing" compensation if a step has no side effect.** Documenting "this step has no compensation needed" is itself the compensation.

2. **Compensations must be idempotent.** They will run on retry. Refunding an already-refunded charge must return the same outcome.

3. **Compensations can fail too.** Plan for it: retry with backoff, then alert humans. "Refund failed, manual intervention needed" is a real production scenario.

4. **Compensations should be commutative where possible.** If compensate(A) and compensate(B) can run in any order without affecting outcome, you have more flexibility on partial failures.

5. **Don't compensate what wasn't done.** Track which steps successfully committed; only compensate those. The orchestration model makes this easy; the choreography model requires careful event tracking.

## State persistence

The saga's state — which steps have committed, which need compensation — must survive crashes. Options:

- **Durable execution engine (Temporal, Inngest)** — state is the engine's job; you write linear code. Best option for any non-trivial saga.
- **Database row per saga instance** — schema like `{saga_id, current_step, completed_steps[], step_outputs{}, status, created_at, updated_at}`. Update transactionally with each step.
- **Event-sourced log** — every step start/complete/fail is an append-only event; current state is the fold. Works well with Kafka or EventStore.

**Anti-pattern**: in-memory state in the orchestrator process. One restart and you've lost track of pending sagas.

## Timeout handling

Sagas often involve human-in-the-loop steps ("waiting for legal approval") that can take days. Without timeouts, sagas can pile up indefinitely.

```
Step "wait_for_legal_approval":
  timeout: 5 business days
  on_timeout: compensate previous steps AND notify deal owner
```

Workflow engines (Temporal `Workflow.sleep`, Inngest `step.sleepUntil`) handle this natively. Manual orchestrators need a scheduled job to scan for expired sagas.

## Saga vs. 2PC

Two-phase commit (2PC) gives strict ACID across systems but requires all participants to support a coordinator protocol, blocks resources during the prepare phase, and falls over under network partitions. In practice, 2PC is rare in modern distributed systems — sagas dominate because they accept eventual consistency in exchange for liveness.

| | 2PC | Saga |
|---|---|---|
| Consistency | Strong (atomic across systems) | Eventual (windows of partial state visible) |
| Liveness under partition | Blocks | Continues |
| Participant requirements | Must implement prepare/commit | Must implement compensation |
| Latency | High (multiple round trips) | Lower (one round trip per step) |
| Real-world adoption | Rare | Dominant |

## Worked example: e-commerce order placement

```
Step 1: validate_cart        compensation: none (read-only)
Step 2: reserve_inventory    compensation: release_inventory
Step 3: charge_payment       compensation: refund_payment
Step 4: create_order_record  compensation: mark_order_cancelled
Step 5: schedule_shipment    compensation: cancel_shipment
Step 6: send_confirmation    compensation: none (best-effort, log if missed)
```

If step 5 fails (shipping API down, all retries exhausted):
- Compensate step 4: mark order cancelled in our DB.
- Compensate step 3: refund the payment (Stripe `Refund.create` is idempotent given the original charge ID).
- Compensate step 2: release the inventory reservation.
- Step 1: no compensation needed.
- Step 6: skip; the order is cancelled, no need to confirm anything.

If step 5 fails but the system recovers, prefer to **retry forward** rather than compensate. Compensation is for permanent failures, not transient ones. The orchestrator's retry policy gates this decision.

## Observability requirements

For each saga instance, capture:
- `saga_id`, `saga_type`, `started_at`, `completed_at`, `final_status`
- Per step: `step_name`, `attempt`, `started_at`, `completed_at`, `outcome`, `error_class`
- Per compensation: same shape, with `is_compensation: true`

Dashboards: success rate by saga type, mean steps to completion, mean compensation rate, p99 saga duration. Alerts: compensation failure (always human-investigate), saga running >24h (likely stuck).

## Anti-patterns

- Compensations that aren't actually inverses ("compensate by sending an apology email" instead of "refund the charge").
- Putting business logic in the compensation that wasn't in the forward step (compensation creates new state).
- Forgetting that compensations can race with retries — both must be idempotent.
- Treating sagas as a replacement for input validation — failing fast before any side effect is always cheaper than compensating.

## Links

- [[relatedTo::Idempotency Patterns for Automation Workflows]]
- [[relatedTo::Workflow Engine Tradeoffs 2026]]
- [[relatedTo::Circuit Breaker Pattern]]
- [[buildsOn::Microservices Patterns (Chris Richardson)]]
