# Payment orchestration service

A synchronous payment service between a restaurant POS and a MarketPay card terminal
(MarketPay Cloud API v1.1.14, pre-production), with **Firestore** as the only store of state.
It implements [`payment-api.yaml`](docs/payment-api.yaml) in Python 3.12 with Flask, pydantic and structlog.

The one promise it keeps: **the waiter gets an answer MarketPay agrees with, within the deadline, and the card is never charged twice**. That holds even when the network loses, delays or duplicates messages, Firestore aborts transactions, or the process dies mid-payment.

- [Quick start](#quick-start)
- [How it works, in one page](#how-it-works-in-one-page)
- [API](#api)
- [Architecture](#architecture)
- [Failure model](#failure-model)
- [Recovery design, and why it's correct](#recovery-design-and-why-its-correct)
- [Crash points](#crash-points)
- [Firestore transaction retries and MarketPay calls](#firestore-transaction-retries-and-marketpay-calls)
- [Payment history](#payment-history)
- [Timeouts the POS must respect](#timeouts-the-pos-must-respect)
- [Model risk](#model-risk)
- [Trade-offs accepted](#trade-offs-accepted)
- [Scope and limits](#scope-and-limits)
- [Testing](#testing)
- [Documentation map](#documentation-map)

---

## Quick start

Requirements: Docker. uv and Python run inside the container.

```bash
cp .env.example .env                                  # MarketPay staging settings, Firestore project
cp <your-firebase-key>.json secrets/firestore-service-account.json
cp POS_NINITO_TEST.crt POS_NINITO_TEST_PRIVATEKEY.key secrets/certs/   # MarketPay mTLS
docker compose up --build                             # the single command
```

- **The service** listens on `http://localhost:8080`. `GET /readyz` checks Firestore;
  `GET /terminals` checks MarketPay (mTLS) and shows whether the terminal is connected.
- **All secrets live in `secrets/`:** the MarketPay mTLS certificate and key in `secrets/certs/`,
  and the Firestore service-account key in `secrets/firestore-service-account.json`. They're mounted read-only into the container, never copied into the image (`.dockerignore`), and never committed (`.gitignore`).
- **Two run modes:**
  - `APP_MODE=prod` (**the default**, what the command above runs, and the one to evaluate and fault-test) runs gunicorn with **one worker and 32 threads**, no reloader. See the `INSTANCE_ID` note under [Scope and limits](#scope-and-limits).
  - `APP_MODE=dev docker compose up` (or `make dev`) runs Flask with live reload, for development only. A code change restarts the process, which is exactly a crash.
- **Tests:** `make test` (runs `pytest` in the container, about 25 s); `make lint` (formatting and lint). CI runs both on every push. `make venv` builds a local `.venv` for IDE autocompletion only.

All configuration comes from environment variables; see [reference: configuration](docs/reference.md#configuration).

---

## How it works, in one page

```
 POS ──POST /payments──▶ ┌────────────────── this service ───────────────────┐
                         │ 1 record intent + lock terminal   (Firestore txn) │
                         │ 2 process-transaction, held open  ──────────────────▶ MarketPay ──▶ terminal
                         │   … customer taps, bank answers   ◀──────────────────   201 result
                         │ 3 record outcome + unlock          (Firestore txn) │
 POS ◀──── approved ──── └───────────────────────────────────────────────────┘
```

**The problem that shapes everything.** A card payment runs on a physical device and takes seconds to minutes. MarketPay's only way to look a payment up is **"the terminal's last transaction"**. Replies can be lost after the money has moved. And on staging a *running* transaction isn't visible at all: `last-transaction` keeps showing the previous one. So the service must never **guess**, never **lose the evidence**, and never **send twice** anything that may have arrived.

**One payment, from the POS's point of view.** `deadlineSeconds` is 60 by default.

1. **Record, then act.** One Firestore transaction stores the payment (id derived from
   `reference`, so a repeat finds it) **and locks the terminal**. A second order for a locked
   terminal gets `409 terminal_busy`. Nothing is sent to MarketPay before this is stored.
2. **Run it, held open.** The service sends `process-transaction` with `waitTime` **past** its
   deadline (150 s), so MarketPay keeps the request open while the customer taps and the bank
   answers.
   **Happy path:** a `201` with the result. One MarketPay call, two Firestore writes, done.
3. **If nobody has finished by 50 s** (the customer hasn't tapped), the service sends
   `abort-transaction` **while its request is still open**. That is the only moment an abort works
   on this terminal. The terminal stops, and the still-open request itself returns the NOK. The
   answer is `failed` by about 51 s, and the terminal is clean. If the customer tapped at the last
   moment (the abort is "too late"), the service keeps waiting for the open request's real answer.
4. **Record the outcome and unlock**, in one Firestore transaction. **The terminal is unlocked
   only together with an outcome MarketPay confirmed.** That's what keeps the evidence (the
   terminal's "last transaction") available until it has been read.
5. **Rare: no answer even by 59 s.** The service answers `unknown` rather than guessing. The
   terminal stays locked, the request keeps listening, and the payment settles the moment
   MarketPay answers, or through `POST /reconcile`. `unknown` is still a known state: the lock
   guarantees nothing else runs on the terminal or overwrites its record, and it tells the waiter
   what to do: **don't re-run the card**.

**Everything else is a variation on those five steps:**
- **A lost reply, a `202`, or an undocumented `4xx`:** watch `last-transaction` (never re-send) → abort → confirm.
- **A request that never left the service** (DNS, connection refused): re-sent with the same
  reference for up to 15 s.
- **A `PARTIAL` approval:** reversed at once, then reported `declined`.
- **The same order submitted again:** returns, or waits for, the existing payment. It's never
  charged twice, and a different amount under the same reference is refused.
- **A crash:** every operation records its owner (this process's boot). When the service
  starts again it runs **one reconcile**, which takes over what the crashed boot left open and
  settles it from `last-transaction`, without re-sending anything that may have arrived. After
  that, `POST /reconcile` or the next request touching a payment does the same. There is no
  background timer.
- **A POS cancel:** aborts a running payment; reverses an approved one (the customer taps again); refunds one that completed despite an abort.
- **Firestore trouble:** retried. After MarketPay has answered, the service returns that answer
  even if the store is down.

Each of these, step by step with every check and timeout: [docs/flows.md](docs/flows.md).

---

## API

The contract ([`payment-api.yaml`](docs/payment-api.yaml)):

| Endpoint                     | What it does                                                                                                                                      |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /payments`             | Take a payment. Synchronous: answers within `deadlineSeconds`. `201` new, `200` existing (same `reference`), `409 terminal_busy`, `400`.          |
| `GET /payments/{id}`         | The stored payment (reads never call MarketPay).                                                                                                  |
| `GET /payments`              | List: filters `state`, `terminalId`, `reference`, `createdAfter`, `createdBefore`; `limit`, `cursor`. Newest first.                               |
| `POST /payments/{id}/cancel` | Abort a running payment / reverse an approved one. `200` with the resulting state, `409` when there's nothing to cancel or it didn't take effect. |
| `POST /reconcile`            | Settle open payments nobody is driving (after a crash, a lost answer). Safe to repeat.                                                            |

Several additions to initial contract to make the assignment implementation & review easier: [docs/api-extensions.md](docs/api-extensions.md). Contract mapping as the brief requires:
- `reference` is sent as `ecrTransactionId` (max 36 characters).
- `deadlineSeconds` drives `waitTime` and every timeout (see [Timeouts](#timeouts-the-pos-must-respect)).
- `terminalId` is passed through as is.
- `SEK` is sent as `"752"` & amounts are sent as strings.

---

## Architecture

Layered the way DDD does it: the domain depends on nothing else in the service, the application
only on the domain, and the adapters plug into the domain's ports. `tests/unit/test_layers.py`
enforces this dependency rule, so a wrong import fails CI.

```
api/                 Interface: Flask handlers parse (pydantic) → call a use case → serialise.
                     Also the MarketPay webhook. No business logic.
application/         Use cases, one module each, behind one facade (service.py › PaymentService):
  take_payment.py      POST /payments: synchronous, within its deadline
  cancel_payment.py    POST /payments/{id}/cancel
  reconcile.py         POST /reconcile
  notifications.py     the bonus webhook's notifications
  queries.py           GET /payments, GET /payments/{id} (stored state only)
  recovery.py          settling operations nobody drives; re-checking inferred outcomes
  undo.py              stop, reverse or refund a charge (each needs the customer's tap)
  store_policy.py      how the use cases write: retry before sending, return MarketPay's truth after
  terminal_ops.py      drives one MarketPay operation to a resolution within a deadline
  context.py, clock.py, notification_inbox.py, errors.py
domain/              Pure rules, no I/O — where the correctness lives, and table-tested:
  models.py            Payment, its states and reasons, the terminal lock
  transitions.py       state changes: (current, now) → next, safe to re-run
  budget.py            the time budget of one operation
  terminal_lock.py     when a payment takes, keeps or releases the terminal
  outcomes.py          reading MarketPay's answers ("status wins", "a timeout is never an outcome")
  abort.py             what an abort's answer tells the service
  history.py           a payment's timeline: which changes get an entry, and what it says
  cancel.py, recovery.py, verification.py, notifications.py, listing.py
  repository.py        port: PaymentRepository (and StoreUnavailable)
  marketpay/           MarketPay's published language: payloads, typed call outcomes, currency,
                       notifications, and the MarketPayGateway port. Pure data.
infrastructure/      Adapters behind the ports:
  store/               Firestore repository, and an in-memory one for tests (same semantics)
  marketpay/           the mTLS HTTP client (MarketPayGateway), signed notification URLs
app.py               Composition root: builds the adapters and wires them into the use cases.
```

The service conforms to MarketPay's own model in the domain (DDD: a conformist relationship)
rather than translating it into its own terms; staging's quirks are handled, and documented,
where they're read.

- **Functional core, imperative shell.** Every decision is a pure function of data (a
  MarketPay answer, the stored payment, the time). The use cases only sequence them: record →
  call → record. Classes exist only where there's state or a collaborator to hold (the HTTP
  client, the store, the clock, the use cases).
- **Typed outcomes, never exceptions for network trouble.** The MarketPay client returns
  `Completed | Accepted | Rejected | Ambiguous | NotSent` (and similar for lookups, aborts and reversals). A timeout is an `Ambiguous` value that no code path can turn into a state.
- **Firestore holds everything.** `payments/{id}` has one document per payment; the id is a UUIDv5 of `reference`. `terminals/{terminalId}` holds the lock (and a verification flag). Every change is a Firestore transaction that applies a pure transition to the **current** stored version.
- **Threads.** Card-present calls run on a worker thread, so a request can abort while MarketPay
  still holds the call open. `last-transaction` calls are serialised **per terminal**, because
  MarketPay answers `500` to every overlapping lookup for one terminal.

Data model, every field, every state and reason: [docs/reference.md](docs/reference.md).

---

## Failure model

What can go wrong, and what the service does. Several rows follow from how staging actually
behaves, measured on the real terminal.

| Failure                                                        | Handling                                                                                                                                                                                                        |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Reply lost after the terminal committed**                    | Never treated as an outcome. Watch `last-transaction` for **this payment's** `ecrTransactionId` (a record of someone else's, or the "one-before-last", never counts) → abort → confirm → `unknown` if still unclear. |
| **Request lost / never left the service** (DNS, TCP/TLS connect) | Re-sent with the same `ecrTransactionId`, backoff 0.5 → 4 s, for ≤ 15 s. Then `failed` / `not_sent` (nothing reached MarketPay).                                                                                |
| **Request delayed, arrives after the service concluded `failed`** | If `failed` was only inferred (no record of this payment), the terminal is flagged. Its next payment (or a reconcile) checks once and corrects the record if a late charge appeared; if the correction can't be stored, that next payment isn't sent (`503`, nothing charged).                                                                         |
| **Duplicated request** (network)                               | The same `ecrTransactionId`. A `400`/`404` is only trusted after a look that would catch a copy that ran. An undocumented `4xx` (e.g. "busy") is treated like a lost reply.                                     |
| **`202` / MarketPay holds longer than expected**               | `waitTime` runs past the service's deadline, so it normally never happens. If it does: watch → abort → confirm.                                                                                                 |
| **Customer slow / never taps**                                 | Normal, not a fault. The abort at the deadline (while the request is open) stops the terminal; `failed` / `aborted`.                                                                                            |
| **Reversal reply lost** (`cancel-transaction`)                 | Never re-sent: a second reversal could answer "NOK, already reversed". Watch `last-transaction` for this reversal's own record (the payment's `ecrTransactionId` with a new `terminalTransactionId`) → `cancelled`. Not seen by the deadline → `unknown`, terminal stays locked; nothing new 180 s after sending → `approved` / `undo_not_recorded`, flagged for a re-check. |
| **Abort too late** (`409`: tapped at the last moment)          | Keep waiting for the open request's real answer. Usually `approved`: the truth wins, it's not reversed.                                                                                                    |
| **`PARTIAL` approval**                                         | Reversed immediately; `declined`, `reversed: true`. If it can't be reversed, the terminal is freed and the payment flagged for a person.                                                                   |
| **Running transaction invisible in `last-transaction`**        | "Not this payment's" never proves "never arrived". Only a finished record of this payment is evidence; a time rule (180 s) settles the rest.                                                                    |
| **Overlapping `last-transaction` → `500`**                     | Lookups serialised per terminal in-process; every lookup is retried and a failed lookup is never an outcome.                                                                                                    |
| **MarketPay `5xx`, resets, timeouts**                          | `Ambiguous`: watch → abort → confirm.                                                                                                                                                                           |
| **Firestore `ABORTED` (contention), outage, timed-out commit** | Retried with backoff (0.1 → 1 s). Before anything was sent: `503` after 5 s (safe to retry). After MarketPay answered: return its answer anyway. A commit that landed but "failed" is recognised on retry. |
| **Process crash / restart mid-flight**                         | Ownership by boot id: the restarted process takes over at once and settles from `last-transaction` (next section).                                                                                              |
| **Duplicate submit from the POS**                              | Same `reference` → the same document: returns it, or waits for the live request, or recovers an orphan. A different amount/terminal/currency → `409 idempotency_mismatch`.                                      |
| **Concurrent activity**                                        | The terminal lock (one operation per terminal); atomic claims (exactly one cancel reverses); atomic take-over (exactly one recovery).                                                                           |

---

## Recovery design, and why it's correct

The design rests on six rules. Each correctness requirement from the brief follows from them.

1. **Evidence is never destroyed before it's read.** MarketPay can only report a terminal's
   *last* transaction. So a terminal is **locked for the whole life of any operation on it**
   (purchase, reversal, refund), and **unlocked only in the same Firestore transaction that
   records an outcome MarketPay confirmed**. No other transaction can start on that terminal
   meanwhile, so the last transaction stays the payment's own until the service has read it.
2. **Intent is recorded before every action.** The payment (and the lock) is stored before `process-transaction`. "Abort requested" is stored before the abort, "undo claimed" before a reversal/refund, and the reversal's baseline before it's sent. After a crash, the stored record says which MarketPay action *might* have happened.
3. **Only MarketPay's own record decides an outcome.** An outcome comes from a result body for the payment's `ecrTransactionId`, or from a `last-transaction` record echoing it (and, for reversals, a new `terminalTransactionId`). A timeout, a `202`, a reset or "not this payment's" is never an outcome.
4. **Nothing that may have reached MarketPay is ever sent again.** Only a request that certainly never left the service (a connect-phase failure) is re-sent. Recovery reads and aborts; it never re-sends. A reversal whose reply was lost is watched for, not repeated, because a second one could answer "NOK, already reversed".
5. **Every change is a pure transition applied to the current stored version**, inside a Firestore
   transaction. It's safe when Firestore re-runs it, and safe when a timed-out commit is retried.
6. **Every operation has an owner and a lease.** An operation is taken over only when orphaned:
   no owner, an expired lease, or **this instance under an earlier boot id** (a restart).
   Take-over is atomic, so exactly one process recovers a payment.

**How the requirements follow:**

| Requirement                                                         | Why it holds                                                                                                                                                                                                                                                                                                       |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **No double charge**                                                | One `reference` = one document = one `ecrTransactionId`; a repeat never sends a second purchase (rule 4). One operation per terminal (rule 1). Only one cancel can claim an undo. A re-send happens only when the earlier attempt never left the service.                                                          |
| **No lost payment** (no `failed`/`cancelled` while a charge stands) | `failed` needs positive evidence: a NOK/refusal record of this payment, or a `204` abort plus no record, or no record long after the terminal's longest transaction (180 s). An inference like that is re-checked once before the evidence can be overwritten. `cancelled` needs the reversal's or refund's own OK record. |
| **No phantom success/cancel**                                       | `approved` and `cancelled` only from MarketPay's OK record for the payment's reference. A reversal record is told apart from the purchase by `terminalTransactionId` and a baseline taken just before sending. A `PARTIAL` is never reported as approved.                                                          |
| **Fast happy path**                                                 | One `process-transaction` held open; no polling, no reversals, no extra lookups. Checks exist only on non-happy branches (a lost reply, a `400`/`404`, a flagged terminal).                                                                                                                                        |
| **A timeout is never an outcome**                                   | Enforced by types: `Ambiguous` / `LookupFailed` have no state mapping, and at the deadline the answer is `unknown`, not `failed`.                                                                                                                                                                                  |
| **Crash consistency**                                               | Rules 1, 2 and 6: the stored intent says what might have happened, the lock preserves the evidence, and a restarted process owns its dead predecessor's payments at once. Recovery ends in a MarketPay-consistent state, or leaves the payment locked and `unknown` until it can.                                  |
| **Known state at the deadline**                                     | The service sends the abort while its request is open → the terminal stops and the open request reports it. Confirmed live on payments and reversals. If even that brings no answer, the reply is `unknown`: the terminal stays locked to this payment, so its record can't be overwritten, and the waiter knows not to re-run the card. It settles when MarketPay answers or on `POST /reconcile`. |

The full recovery procedure, per operation: [docs/flows.md › Crash recovery and reconcile](docs/flows.md#3-crash-recovery-and-reconcile).

---

## Crash points

"Stored" is what Firestore holds when the process dies. Convergence is what the restarted
process does: first its one reconcile at start, then `POST /reconcile`, a POS retry of the same order, a cancel, or the next payment on that terminal. Reconcile spends at most 90s on one payment; anything still unclear converges on a later call.

| Crash while…                                        | Stored                                                          | Converges to                                                                                                                                                   |
| --------------------------------------------------- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| creating the payment (before the Firestore commit)  | nothing                                                         | Nothing happened. The POS retry is a fresh payment.                                                                                                            |
| after the commit, before `process-transaction` left | `pending`, locked                                               | Watch → abort (idle: `409`) → no record → after 180 s `failed` / `never_recorded`, flagged for a later check.                                                  |
| waiting for the card / the bank                     | `pending`, locked                                               | The terminal ends it on its own (~120 s card timeout; an abort can't stop it once the request died). Watch → its NOK/OK record → `failed` / `approved`.        |
| after MarketPay approved, before the service recorded it | `pending`, locked                                               | The first look finds the payment's OK record → `approved`.                                                                                                     |
| sending the abort                                   | `pending`, `abort_requested_at`, locked                         | As above; a later approval is the truth.                                                                                                                       |
| recording the outcome (Firestore failed)            | `pending`, locked                                               | The same truth from `last-transaction`. The POS already got the right answer.                                                                                  |
| a reversal was claimed / in flight                  | `approved`, `operation=reversal`, `undo_started_at`, baseline   | **Never re-sent.** Watch for its record (the payment's reference + a new id) → `cancelled`, or after 180 s with nothing new → `approved` / `undo_not_recorded`, flagged. |
| a refund in flight                                  | `approved`, `operation=refund`, `refund_reference`              | Watch the refund's own reference → `cancelled` / `refunded`, or not done → `approved`.                                                                         |
| a refund due but never sent                         | `approved`, `operation=refund`, no `undo_started_at`            | **Not started by recovery** (needs a tap): released as `approved` / `refund_due`; the POS cancels again.                                                       |
| a PARTIAL's reversal due but never sent             | `unknown`, `operation=reversal`, `undo_reason=partial_approval` | Released for a person: `unknown` / `partial_not_reversed`, terminal freed.                                                                                     |

---

## Firestore transaction retries and MarketPay calls

Firestore transactions are serializable, but under contention they abort and re-run the transaction body, up to 5 times in the client library, then fail. The design handles that:

- **No MarketPay call ever happens inside a transaction body.** Bodies only read → decide (a pure  function) → stage writes. A re-run has no side effect.
- **Transitions decide from the current stored version,** never from a copy the request held earlier. So the payment request and a cancel (or a recovery) can't overwrite each other.
- **Idempotent under retry.** A write can time out after it landed. Retrying the same transition sees its own result and changes nothing. Where "unchanged" would be misread, the code checks
  "was that me?":
  - creating the payment uses `created_by`, a request id;
  - claiming an undo and taking over check the owner.
- **Retrying the store is the service's job too.** The library gives up after 5 aborted attempts (raising `ValueError`); `DeadlineExceeded` and `ServiceUnavailable` aren't retried at all. The service translates all of them into one transient error and retries with backoff within the request's budget.
- **The order around MarketPay calls is always:** commit the intent → call MarketPay → commit the outcome. A crash between any two leaves a record that says what might have happened (rule 2).

---

## Payment history

**Why it exists.** A payment document holds only its *current* state: every transition
overwrites it. That is all the service needs to be correct, but a person asking "why did the
waiter see `unknown`, and when did it become `approved`?" would have to dig through logs. So
each payment also keeps a short timeline, one line per change of its state, its reason, or what
holds the terminal for it:

```
GET /payments/{id}/history
 #1 pending   pending: payment started; terminal held for the purchase
 #2 unknown   unknown (awaiting result), after the service's abort
 #3 approved  approved (bank approved), found in last-transaction; terminal released
```

It is stored in Firestore as `payments/{id}/history/{entry}`. The entry ids sort in order
(`0001_2026-09-28T12:32:51.579000Z`), so the Firestore console shows the timeline as-is.

**Why is it needed.** Each entry is written in the **same transaction** as the change it
describes, so there is never an entry for a change that didn't commit, nor a change without its
entry. A transaction Firestore re-runs, or a retry after a lost commit, rewrites the same entry
rather than adding one. The fault suite checks this after every scenario, crashes included.
Coordination alone (a take-over, a lease, a write-ahead note) gets no line.

**A deliberately simple example.** It shows how a payment's trail can be kept alongside its
state; it is not a complete audit trail. A production-grade system would likely need more:
- **who** caused each change (which request, reconcile run, operator), and **why** (the POS
  call, the MarketPay answer it rests on);
- the **MarketPay calls** themselves (request and response, redacted), not only the state they
  led to;
- **retention** rules and immutability guarantees (e.g. append-only storage, export to a
  warehouse), as payment regulations and disputes may require;
- querying **across** payments ("every payment that went `unknown` today"), which belongs in a
  log or analytics store rather than in per-payment subcollections.

Everything else that happens (each MarketPay call with its timing, retries, recoveries) is in the
structured logs, which carry `payment_id` and `request_id` on every line.

---

## Timeouts the POS must respect

- **`POST /payments`: the POS read timeout must be longer than `deadlineSeconds`.** Use at least `deadlineSeconds + 5 s`. The service answers by `deadlineSeconds − 1 s`, and that answer is the only definitive one. A POS that gives up earlier sees "no answer" for a payment that may be approved a second later, and the waiter will run the card again. That's the double charge this whole service exists to prevent.
- **`POST /payments/{id}/cancel`: at least 130 s.** A cancel can take up to about two minutes: up to 59 s to stop a running purchase, then, if it completed anyway, up to 59 s for a refund, which needs the customer's tap. A reversal alone takes up to 59 s.
- **`POST /reconcile`: allow up to 90 s per unresolved payment.** It handles them one by one;
  there's normally at most one per terminal.
- **Deadlines under ~20 s leave no time for a customer to tap.** They still end in a known state:
  the abort is sent halfway, and the answer comes 1 s before the deadline. For 3 s or less, the
  abort goes at a third and the answer at two thirds, so even a 1 s deadline stops the terminal.

---

## Model risk

MarketPay is a black box, so live experiments measured how staging really behaves. Staging differs from the spec in ways the design had to follow:
- running transactions are invisible;
- abort works only while the service's request is open, and `409` means nothing;
- a reversal is a card-present refund;
- a reversal shows up as a normal result with a new id and no `cancellationResult`;
- overlapping lookups fail;
- NOK results omit fields the spec calls required;
- **`transactionType` is never echoed back**, not even on approvals. So the one field that should
  tell a purchase from its reversal can't be used, and the service tells them apart by
  `terminalTransactionId` instead.

**Before trusting this in production, confirm with MarketPay** (full list with the service's
assumptions and what breaks if they're wrong: [open-questions.md](docs/open-questions.md)):
1. **The longest a transaction can stay unrecorded** (card, PIN, slow issuer). The service's
   time rule assumes 180 s.
2. **That a `204` abort guarantees the transaction can't complete.**
3. **Whether `ecrTransactionId` is deduplicated.** What happens if a completed one is sent again?
   The service never does; a network duplicate might.
4. **Which `4xx` a busy terminal returns.** A `404` there would break current "404 = nothing started".
5. **Whether `IN_PROGRESS` / `cancellationResult` ever appear, and whether staging reverses and refunds the same way production does.**
6. **Whether sending the original amount reverses a `PARTIAL` in full.**
7. **The one-lookup-per-terminal limit**: does it apply to other endpoints, and to other
   clients of the same terminal?
8. **Whether staging approves through a simulated host** (the constant `authorizationCode`).
9. **Whether production echoes `transactionType`**.

---

## Trade-offs accepted

| Decision | Accepted cost |
|---|---|
| Report the truth over the deadline: an approval after a "too late" abort stays `approved` | The waiter may get `unknown` at the deadline instead of a clean `failed`. This can be fixed after several open questions with MarketPay are resolved. |
| Reverse a `PARTIAL` and report `declined` (a reversal by id, never a refund of an unknown amount) | The customer pays again with another card; an unreversible partial needs a person. |
| One `reference` = one attempt | Retrying a declined order needs a new reference. |
| NOK without an acquirer code → `failed`, not `declined` | A production bank decline without a code would make the waiter retry the same card. |
| Recovery never starts a card transaction | A refund that was due is released as `approved` / `refund_due`; the POS must cancel again. |
| One process per `INSTANCE_ID` | Throughput from threads, not processes. |
| `status` beats `responseCode` (contradictions logged as errors) | If `status` were ever the wrong field, a record could be wrong until a person checks. |
| On a store outage after MarketPay answered, return its answer | Reads can lag behind what the POS was told, until reconcile. |
| `GET /payments` needs no Firestore composite index | Sparse filters over long histories page through short pages. |
| Correct late surprises in the record, never reverse them automatically | A late charge needs a person to refund it. |
| Reads never ask MarketPay | An unresolved payment reads `unknown` until reconcile runs. |
| One `PURCHASE` per payment, **not** pre-authorisation + completion (the API offers both) | Amounts are final at the tap (tips are part of the purchase). A pre-auth would only reserve the money, and add a second terminal transaction with its own lost-reply and crash cases. |
| The brief's example: **auto-reversing a slow-but-approved payment** | The service **doesn't**. A late approval stands, so the waiter isn't forced to re-run a paid order. |

Details and reasoning: [open-questions.md › Decisions](docs/open-questions.md#decisions-made-recorded-here-with-the-trade-off).

---

## Scope and limits

Everything the brief requires is built, including the bonus. What follows are the deliberate
boundaries of that work, each with its reason. Ideas for going further (message queues, resilience, security) are in
[docs/future-improvements.md](docs/future-improvements.md).

- **Faults are injected against a simulated terminal, not live traffic.** The project's fault suite
  models the behaviour observed on staging and checks the invariants against the terminal's
  own ledger (see [Testing](#testing)). Dropping, delaying and duplicating real MarketPay
  traffic through a proxy is what an external fault suite does; this suite covers
  the same faults deterministically.
- **Covered by tests, not reproducible on staging:** the REFUND path (it needs an approval
  that lands exactly as the service's abort arrives), a `PARTIAL` (staging never produces one),
  and a request delayed long enough to reach the terminal after the service reported `failed`.
- **Contracts that live outside the service:**
  - the POS timeouts (above) are set in the POS: this README states them and explains why, and a
    server can't enforce a client's timeout;
  - `INSTANCE_ID` must be unique per running process, a deployment rule. The shipped
    entrypoint meets it: one gunicorn worker, many threads.
- **Lookups are serialised per process.** MarketPay answers `500` to overlapping
  `last-transaction` calls for one terminal, so the service queues its own. A caller outside the
  service polling the same terminal at the same moment (another instance, a test suite checking
  the service's records against MarketPay) makes both calls fail: the service retries its own and
  a failed lookup is never an outcome, but that caller should retry too.
- **Reconcile works through payments one at a time.** A terminal has at most one open
  payment, so with one terminal there's nothing to parallelise. For a fleet, the path is one
  reconcile per terminal in parallel, since terminals are independent.
- **The notification webhook (the bonus)** is built and tested with the real notifications
  captured on staging (an approval and a card timeout). MarketPay doesn't authenticate its
  notifications: the service's signed, secret URLs are the protection it controls, and in
  production an IP allowlist or a MarketPay signature check belongs on top of them.
  The inbox that hands a notification to the request waiting for it is in memory, per process:
  across a restart, or on another instance, polling settles the payment instead.

---

## Testing

Run: `make test` (all 438, about 25 s). The tests are grouped by kind; one group alone runs
with e.g. `docker compose run --rm --no-deps api uv run pytest tests/unit`:

| Folder | What | Tests |
|---|---|---|
| `tests/unit/` | pure rules and models, no app: decisions, budgets, lock, recovery, cancel, verification, notification and history rules, and the layer dependency rule | 155, in milliseconds |
| `tests/api/` | one feature per file, through the HTTP API: payments, abort, cancel, `PARTIAL`, reconcile, store failures, locks, idempotency, listing, history, notifications, health, terminals | 230 |
| `tests/concurrency/` | races between real threads: cancel vs. payment, cancel vs. cancel (incl. a double-tapped cancel), the lock, overlapping lookups | 12 |
| `tests/faults/` | the fault suite (below) | 41 |
| `tests/support/` | helpers, not tests: the scripted fake MarketPay with the real payloads captured on staging (`marketpay_fakes.py`), and the simulated terminal with a crashable service (`terminal_sim.py`) | — |

The API tests run against a **scripted fake MarketPay** (an `httpx.MockTransport`) that can lose, delay, refuse and duplicate answers, block until an abort arrives (like the real terminal), and **crash the process mid-call**. The crash is a `BaseException` that nothing catches; a "restarted" service with a new boot id then reconciles. They also run against an **in-memory store** with Firestore's semantics, which can fail on demand or "lose" a commit response. A fake clock makes minutes of polling instant.

**The fault suite** (`tests/faults/`, with `tests/support/terminal_sim.py`) doesn't trust the tests' own expectations. It runs the brief's faults against a **simulated terminal** that models what
staging does (a held-open call, abort only while it's open, a 202 and a running transaction
hidden from `last-transaction`, a reversal as a new record) and keeps its **own ledger** of
every charge. After each scenario it checks the service's records, and every answer the POS got,
against that ledger:
- no double charge: at most one standing charge per order, and no refund of money that
  wasn't charged;
- no lost payment and no phantom cancel: `failed`, `declined` or `cancelled` means no standing
  charge;
- no phantom success: `approved` means exactly one;
- converged: nothing left `pending` or `unknown`, and the terminal is free.

Scenarios: every customer behaviour (taps, declined, never taps, taps as the abort arrives,
slow with a `202`, partial); a request that never left, was lost, was delayed until after the
service said `failed`, or whose response was lost; failing lookups; duplicate and concurrent submits;
cancels, with lost, unsent and never-arriving reversals, a refund after an abort, a customer
gone before the reversal; Firestore contention, a lost commit, the store down after the
charge; and crashes before and after sending the payment or the reversal, while the customer
is at the terminal, and while recording the outcome, each followed by a restart and
reconcile (or the POS retrying). It was checked by planting bugs (re-sending after a lost
answer, taking a timeout as a failure, skipping the re-check of an inferred failure): each
one fails the suite.

CI (`.github/workflows/ci.yml`) runs formatting, lint and all tests on every push, to any
branch. It needs no secrets: MarketPay and Firestore are simulated.

---

## Documentation map

| Document | What's in it |
|---|---|
| [docs/flows.md](docs/flows.md) | **Every flow, step by step**: every check, timeout and branch, with the log events each one emits. |
| [docs/reference.md](docs/reference.md) | **Every** state, reason, field, lock rule, timeout, setting, MarketPay call and log event. |
| [docs/open-questions.md](docs/open-questions.md) | Questions for MarketPay, the design decisions and their trade-offs. |
| [docs/known-issues.md](docs/known-issues.md) | Open issues: none. Every bug found during development was fixed and covered by tests. |
| [docs/future-improvements.md](docs/future-improvements.md) | Ideas beyond the assignment |
| [docs/api-extensions.md](docs/api-extensions.md) | What the API adds to or interprets in the contract. |
| [docs/examples/](docs/examples/README.md) | **Request/response examples** for every endpoint: the service's (generated from the running code) and MarketPay's (observed on staging). |
