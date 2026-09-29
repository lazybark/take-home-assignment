# Flows

Every flow the service runs, step by step: each check, each timeout, each branch, and the log
events it emits. It is written for someone who has never seen the code. File references point to
where each step lives.

**Conventions**
- **D** = the request's `deadlineSeconds` (default 60, 1–120). Times are measured **from the
  moment the request arrived**, on a monotonic clock.
- **A look** = one `GET /last-transaction/{terminal}` call, at most 5 s. Looks for one terminal
  never overlap (MarketPay fails overlapping ones with `500`), and a failed look is never an outcome.
- **Its own record** = a `last-transaction` record whose `finalTransactionParams.ecrTransactionId`
  equals the payment's `reference`, in state `FINISHED`. Anything else is "not its own": empty,
  someone else's transaction, the "one-before-last" the spec warns about, or a running
  transaction, which staging doesn't show.
- **Stored:** every change to a payment is one Firestore transaction that applies a pure
  transition to the *current* stored version (`domain/transitions.py`). The terminal lock is
  acquired or released in the same transaction (see [reference › Terminal lock](reference.md#terminal-lock)).
- **Log events** are structlog events. Every line of a request carries `request_id`,
  `payment_id`, `reference` and `terminal_id`. `marketpay_call` is logged when a MarketPay call
  *finishes*, so for a call that was held open it appears after the events that happened while it
  was open.

Contents:
1. [Take a payment](#1-take-a-payment--post-payments)
2. [Cancel a payment](#2-cancel-a-payment--post-paymentsidcancel)
3. [Crash recovery and reconcile](#3-crash-recovery-and-reconcile)
4. [The one-look recheck](#4-the-one-look-recheck)
5. [Re-checking inferred outcomes](#5-re-checking-inferred-outcomes)
6. [Reads](#6-reads)
7. [When Firestore fails](#7-when-firestore-fails)
8. [Event sequences](#8-event-sequences)
9. [MarketPay notifications (webhook)](#9-marketpay-notifications-webhook)

---

## 1. Take a payment — `POST /payments`

`application/take_payment.py › TakePayment`, `application/terminal_ops.py › run_transaction`

### 1.1 Validate the request → `400 validation_error`

| Field | Rule |
|---|---|
| `terminalId` | `MANUFACTURER:serial`: `^[A-Z0-9]+:[^/\s]+$` (uppercase manufacturer, no `/`, no spaces), at most 128 characters |
| `amount` | integer ≥ 1 (minor units, öre) |
| `currency` | `^[A-Z]{3}$` and known: `SEK`→`752`, `EUR`→`978`, `DKK`→`208`, `NOK`→`578`. The terminal approves SEK only; any other currency is refused by MarketPay with a `404`. |
| `reference` | 1–36 characters (sent as `ecrTransactionId`) |
| `deadlineSeconds` | 1–120, default 60 |

The body must be a JSON object.

### 1.2 The time budget (`domain/budget.py › process_budget`)

| | Formula | D = 60 | D = 20 | D = 120 | D = 8 |
|---|---|---|---|---|---|
| `waitTime` sent to MarketPay | min(300, D + 90) | 150 s | 110 s | 210 s | 98 s |
| the service's read timeout for that call | `waitTime` + 5 | 155 s | 115 s | 215 s | 103 s |
| **abort point** | max(D − 10, D / 2), at most answer by − m | **50 s** | **10 s** | **110 s** | **4 s** |
| **answer by** | D − m, with m = min(1 s, D / 3) | **59 s** | **19 s** | **119 s** | **7 s** |

For a deadline of 3 s or less, m is a third of it: the abort goes at D / 3 and the answer at 2D / 3,
so even a 1 s deadline stops the terminal while the request is open.

`waitTime` deliberately runs **past** the deadline. An abort only stops the terminal while
MarketPay still holds the service's request open: if MarketPay had already answered `202`, the
abort would get a useless `409`.

### 1.3 Record the intent and lock the terminal (one Firestore transaction)

A candidate payment is built:
- `id` = UUIDv5 of `reference`, so the same order always maps to the same document;
- `state = pending`, `operation = purchase`;
- `owner` = this process (`INSTANCE_ID` and this boot's random id);
- `lease_until` = now + D + 30 s, and `deadline_at` = now + D;
- `created_by` = a random id for this request.

In one transaction the store reads the payment document and the terminal document, then decides
(`decide_begin`):

| Found | Decision | What's written |
|---|---|---|
| no payment with this id; terminal free (or locked by this very id) | **CREATED** | the payment + the terminal lock (and the terminal's verification flag is read out) |
| a payment with this id, created by **another** request | **DUPLICATE** | nothing → 1.9 |
| a payment with this id, created by **this** request (`created_by` matches: this request's own commit landed, only its response was lost) | CREATED | (already there) |
| the terminal locked by **another** payment | **TERMINAL_BUSY** | nothing → 1.4 |

Firestore errors are retried with backoff for up to 5 s. If the store stays down: **`503
store_unavailable`**. Nothing has been sent to MarketPay, so repeating the request is safe.

### 1.4 The terminal is busy

The payment holding the lock (the "blocker") gets **one look** (§4), but only if nobody is
driving it: it's `unknown`, or its owner is dead. If that look settles it, and so frees the
terminal, the transaction in 1.3 is tried once more. Otherwise the answer is **`409
terminal_busy`**, naming the blocker's reference.

### 1.5 Check the previous payment's inferred outcome (only if the terminal is flagged)

If the previous payment on this terminal ended on "no record of it" (§5), one look happens now,
**before** this payment's transaction overwrites the evidence. A late charge or reversal is written
into the old record, the flag is cleared, and the service goes on. A normal payment never takes
this step.

If a correction was found but **can't be stored** (or the store can't be read), this payment is
**not sent**: sending would overwrite the only evidence. It is released as `failed` / `not_sent`
(nothing charged), the answer is `503`, and the flag stays for a later look.

### 1.6 Send and wait

1. **Send** `process-transaction` on a worker thread: `PURCHASE`, amount as a string, the numeric
   currency, `ecrTransactionId = reference`, `ecrParams.ecrId`, and the `waitTime` from 1.2.
2. **Wait** for the call until the **abort point**.

**The call answered before the abort point.** What it answered decides the next step:

| Answer | Meaning | Next |
|---|---|---|
| `201`, `status OK` | approved | → record `approved` / `bank_approved` (a `responseCode` other than `000` is noted and logged) |
| `201`, `status NOK` + `responseCode` | the bank declined | → record `declined` / `bank_declined`, `declineReason` = the code (`000` here is noted and logged: `status` wins) |
| `201`, `status NOK`, no code | stopped before the bank (Cancel pressed, no card in time) | → record `failed` / `terminal_stopped` |
| `201`, `status PARTIAL` | an approval of an unknown, smaller amount | → record, then reverse it (1.8) |
| `201` echoing another `ecrTransactionId`, or with no status | not a usable answer about this payment | → record `unknown` (`not_our_transaction` / `unrecognised_result`); stays locked; settled later (§3) |
| **connection failed before sending** (DNS, TCP/TLS connect, connect/pool timeout) | MarketPay never saw it | **re-send** the same request: backoff 0.5 → 1 → 2 → 4 s, for at most 15 s and never past the abort point. Still failing → `failed` / `not_sent`. |
| `400` or `404` | refused before anything started (bad request / terminal offline / currency) | **one look** (2 attempts, 1 s apart): if **its own** transaction finished (a network-duplicated copy ran), that's the outcome; otherwise `failed` / `provider_rejected` (`HTTP 404; no record of ours`) |
| any other `4xx` (e.g. `409`, `429`) | undocumented; might mean "busy with a copy of yours" | treated as a lost answer ↓ |
| `202`; read timeout; reset after sending; `5xx`; an unreadable `201` | **no answer** — it may be running or done | **watch → abort → confirm** ↓ |

**Watch → abort → confirm** (`settle_transaction`, used whenever the call itself proves nothing):
1. **Watch:** a look about once a second until the abort point. It stops as soon as the payment's
   **own record** appears; its result is the outcome. "Not its own" means nothing, and never
   triggers a re-send: a running transaction looks exactly like that.
2. Store "abort requested" (best effort), then **abort**: at most 3 attempts while there's no
   usable answer, each ≤ 5 s.
3. **Confirm:** looks until "answer by". Stop when the payment's **own record** appears, or when
   the abort answered `204` and two looks in a row show "not its own".
4. Decide (`resolve_after_abort`):

| Abort answer | Last look | Outcome |
|---|---|---|
| any | **its own record**, finished | that record's outcome. NOK without a code after a `204` → `failed` / `aborted`; an approval stays `approved`: the truth wins over the deadline |
| `204` | not its own | `failed` / `aborted`, via `abort_response`. **Inferred**, so the terminal is flagged (§5). |
| `409` / refused / no answer | not its own, running, or no successful look | `unknown` / `awaiting_result` (on staging a `409` proves nothing) |

**The call is still open at the abort point** (the customer hasn't tapped):
1. Store "abort requested" (best effort).
2. **Abort while the call is open**: 3 attempts at most. On staging this gets `204`.
3. Wait for the call until "answer by":
   - **It answers** (normally about 1 s after a `204`): classify it as in the table above. A NOK
     without a code after the `204` to the service's abort becomes `failed` / `aborted`. If the
     abort was "too late" (`409`, the customer just tapped), the answer is whatever the bank said,
     usually `approved`.
   - **Still no answer at "answer by":** `unknown` / `awaiting_result` ("request still open at
     the deadline; abort …"). The call **keeps running**. When it finally answers, the late
     result is recorded straight away (1.10).

### 1.7 Record the outcome (one Firestore transaction, `apply_purchase`)

- Only applies while the payment is still `operation = purchase`. If something else settled it
  meanwhile (a recheck, a cancel, a late answer), the stored version wins.
- A **final** outcome (`approved`, `declined`, `failed`, `cancelled`) clears the operation, which
  **releases the terminal lock in the same transaction**. `unknown` keeps it.
- The owner is cleared either way: an operation still unresolved is now "orphaned", so the next
  explicit action may take it over (§3).
- **If the POS asked to cancel meanwhile** (`cancel_requested_at` set):
  - stopped before the bank → `cancelled` / `cancelled_before_charge`;
  - approved anyway → the terminal stays locked with a **refund due** (§2.4).
- A `PARTIAL` becomes a **reversal due** (1.8).
- **If the store fails here:** retry until just before the deadline. Then **answer with
  MarketPay's outcome anyway** and log `store_write_failed_returning_marketpay_outcome`. The
  stored record still holds the lock and converges through reconcile. A charge that happened
  is never answered with a `500`.

### 1.8 A `PARTIAL` approval

The payment stays `unknown` and keeps the terminal. It is reversed **now, within this request's
deadline**, while the customer is still at the terminal (the reversal needs their tap). The
steps are those of a POS reversal (§2.3). It is **always** by `terminalTransactionId`, **never a
refund**, because a refund names an amount and MarketPay never says how much was approved.
Until it has claimed the reversal, this request stays the payment's owner, so no look from
elsewhere releases the reversal meanwhile.

A PARTIAL learned **after** its request ended (a late answer, a look) has no owner. A POS
cancel still reverses it, since the customer is there. If another payment needs the terminal
first, it is released as `unknown` / `partial_not_reversed`, for a person (recovery never starts
a card transaction).

| Result | Stored | Terminal |
|---|---|---|
| reversed | `declined`, `reversed: true`, `partial_approval_reversed`, `declineReason` = MarketPay's code | freed |
| refused / not sent / no time left / no `terminalTransactionId` | `unknown`, `partial_not_reversed`: **a person must settle it** | freed (its record is final) |
| sent, not visible by the deadline | `unknown`, `awaiting_undo` | locked; settled later (§3) |

### 1.9 The same `reference` again (a duplicate submit)

1. **Different order:** the stored payment has a different `terminalId`, `amount` or `currency`
   → **`409 idempotency_mismatch`**, and the terminal isn't touched. (`deadlineSeconds` may differ:
   it describes the request, not the order.)
2. **Already settled:** returned as is → **`200`**.
3. **A live request is still driving it:** wait (re-reading Firestore every 0.5 s) until it
   settles, or until *this* request's "answer by" → **`200`** with the real outcome, not
   `pending`.
4. **Its request is dead** (orphaned, §3): take it over and run the full recovery within *this*
   request's budget → **`200`**. This is the POS retrying after the service crashed.
5. Finally, one look (§4) if it's still unresolved.

### 1.10 A late answer

If the held-open call answers after the service already replied, its result is recorded when it
arrives (`late_answer` → `late_answer_recorded`), using the same `apply_purchase`. If a recheck
or reconcile settled the payment meanwhile, that write changes nothing.

### 1.11 What `POST /payments` answers

| Status | When |
|---|---|
| `201` + Payment | a new payment, in its resolved state (`approved` / `declined` / `failed` / `cancelled` / rarely `unknown`) |
| `200` + Payment | the same order again (1.9) |
| `400 validation_error` | invalid body (1.1) |
| `409 terminal_busy` | another payment holds the terminal (1.4) |
| `409 idempotency_mismatch` | the reference already names a different order (1.9) |
| `503 store_unavailable` | Firestore down before anything was sent: safe to repeat |

---

## 2. Cancel a payment — `POST /payments/{id}/cancel`

`application/cancel_payment.py › CancelPayment`, `application/undo.py`, `domain/cancel.py`

A cancel has its own budget: **59 s per phase** (`CANCEL_DEADLINE_SECONDS` = 60, minus 1). A
cancel that must first stop a purchase and then refund it runs two phases. The POS timeout is
therefore ≥ 130 s (see [README › Timeouts](../README.md#timeouts-the-pos-must-respect)).

### 2.1 Plan

The payment is read (a `404` if unknown), gets one look (§4) if nobody is driving it, and, if
its outcome was only inferred (e.g. "the reversal never landed"), one re-check (§5): a late
reversal is found there instead of being sent a second time. Then:

| Payment | Plan (`plan_cancel`) |
|---|---|
| `cancelled` | **already cancelled** → `200` (safe to repeat) |
| `operation = purchase` (still running, or `unknown`) | **stop the purchase** (2.2) |
| `operation` = reversal/refund, already sent | **wait for that undo** (2.5) |
| `operation` = reversal/refund **due, not yet sent** | **undo**: claim it (2.3/2.4) |
| `approved`, nothing in flight | **undo** (2.3/2.4) |
| `declined` / `failed` | **not cancellable** → `409 not_cancellable` |
| `unknown`, nothing in flight (e.g. a partial) | **needs attention** → `409 needs_attention` |

**How an approved payment is undone** (`undo_operation`):
- **REFUND** if an abort was ever sent for it (MarketPay: a payment that completed despite an
  abort is undone with a refund), or if the service never learned its `terminalTransactionId`;
- **reversal** (`cancel-transaction`) otherwise;
- a `PARTIAL` is always a reversal.

### 2.2 Stop a running purchase

1. Record "cancel requested" and "abort requested" (a Firestore failure here → `503`, since
   nothing has been sent). If the purchase **settled** between the cancel's read and this write,
   stop: an abort names no transaction and could stop the *next* payment on the terminal. An
   approved one is then undone (2.3).
2. **Abort** (≤ 3 attempts). If the purchase's own request is still open, this gets `204`, and
   that request returns the NOK within about a second.
3. **If that request is alive,** wait (re-reading every 0.5 s) until 49 s for it to record the
   outcome. Because a cancel was requested, "stopped" becomes **`cancelled` /
   `cancelled_before_charge`**, and "approved anyway" leaves a **refund due**.
4. **If nobody is driving it** (it was `unknown`, or its request died): **take it over**
   atomically, confirm (the looks from 1.6, until 59 s) and record.
5. If it ended approved with a refund due → **undo** it (2.4) with a fresh 59 s budget.

### 2.3 Reverse (`cancel-transaction`)

1. **Claim** (one Firestore transaction, `claim_undo`), which only one request can win:
   - it sets `operation = reversal`, `owner`, and a lease (now + 60 + 30 s);
   - it records `undo_started_at`, bumps `undo_attempts` and records why (`pos_cancel` /
   `partial_approval`);
   - it **takes the terminal lock**. Another payment holding the terminal → **`409
     terminal_busy`**, and nothing is sent.
   - Losing the claim (a concurrent cancel won it) → wait for theirs (2.5). If "unchanged" turns
     out to be the request's own claim whose commit reply was lost, the request carries on. A
     claim records the request's own token, so two requests of one process (a double-tapped
     cancel) can't both win it: the process id alone can't tell them apart.
2. **Baseline:** one look (≤ 3 attempts, 1 s apart). The service records the `terminalTransactionId`
   of the terminal's last record *now* (write-ahead, best effort). While the service holds the lock
   nothing else runs, so whatever is last now can't be this reversal. This matters for a second
   attempt: the terminal may still show the first attempt's record.
3. **Send** `cancel-transaction` on a worker thread: the original `terminalTransactionId`,
   `ecrTransactionId`, amount and currency, with `waitTime` past the deadline. **The terminal asks
   the customer to tap again** (on staging, a reversal is a card-present refund).
4. **Wait** until the abort point (49 s). Still open (nobody tapped) → **abort while it's open**
   (`204`, then the call returns a NOK) → wait until 59 s.
5. Decide:

**A repeated attempt** (after an earlier one ended "not done") is sent only once its baseline is
known **and** stored: its record carries the payment's `ecrTransactionId` and a new id, just like
the earlier attempt's, so without the baseline the two can't be told apart. Otherwise it isn't
sent (`undo_not_sent`, the charge stands, the terminal is freed; a later cancel may try again).

| Answer | Result |
|---|---|
| `200`, cancellation `OK` | **done**: `cancelled`, `reversed: true`, `reversed` |
| `200`, cancellation `NOK` (refused, or nobody tapped and the service aborted) | **not done**: the charge stands → `approved`, `undo_refused`, terminal freed; a later cancel may try again |
| `200`, cancellation `PARTIAL` | **partial**: `unknown`, `partial_reversal`, terminal freed → needs a person |
| never left the service | sent at most 3 times in all (2 re-sends), 1 s apart → then **not done** (`undo_not_sent`) |
| `4xx` | **not done** (`undo_refused`) |
| `202`, lost reply, still open at 59 s | **watch** (never re-send: a second reversal could answer "NOK, already done") until 59 s for **this reversal's record**: the payment's `ecrTransactionId` with a **new** `terminalTransactionId`, neither the purchase's nor the baseline's (staging reuses the payment's `ecrTransactionId` for the reversal); or, in the spec's shape, a `cancellationResult` for this payment. Seen → its status. Not seen → **unclear**: `unknown`, `awaiting_undo`, **terminal stays locked** (§3/§4 settle it). |

For a `PARTIAL` (1.8) the same results are mapped differently: done → `declined`; not done or
partial → `unknown` / `partial_not_reversed`.

### 2.4 Refund (`process-transaction`, `transactionType: REFUND`)

After a claim like 2.3 (with `operation = refund`), a **new** `ecrTransactionId` is used for each
attempt: `rf01<payment-id hex>`, `rf02…` (MarketPay treats the reference as an idempotency key).
It runs exactly like a purchase (1.6), including the abort-while-open, the re-send of what never
left and the late answer, with a deadline equal to the phase's remaining time. Its outcome:
- an approved refund → **done**: `cancelled`, `refunded`, `reversed: true`;
- any other final outcome → **not done**: `approved`, charge stands;
- `unknown` → **unclear** (terminal stays locked).

The customer must tap for a refund too (untested live).

### 2.5 Wait for an undo already sent

Re-read every 0.5 s, until 59 s, while its request is alive. If that request is dead (orphaned):
take the undo over and recover it (§3). Then one look (§4).

### 2.6 What the cancel answers

| Status | When (`cancel_payment.py › _cancel_kind`) |
|---|---|
| `200` `cancelled` | reversed, refunded, stopped before the bank, or already cancelled |
| `200` `declined` / `failed` | it ended without a charge while the cancel stopped it (e.g. the bank declined at the same moment), or a partial was reversed |
| `200` `unknown` / `pending` | an outcome not visible yet (terminal kept locked) |
| `409 not_cancellable` | declined/failed before the cancel did anything |
| `409 cancel_failed` | the reversal/refund definitively didn't happen; **still approved** |
| `409 needs_attention` | partial reversal or partial approval: a person must settle it |
| `409 terminal_busy` | another payment holds the terminal; nothing sent |
| `404` / `503` | unknown id / store down before anything was sent |

---

## 3. Crash recovery and reconcile

`application/reconcile.py › Reconcile`, `application/recovery.py › take_over_and_recover`, `domain/recovery.py`

### 3.1 Who may take over an operation (`is_orphaned`)

An operation is orphaned, meaning nobody alive drives it, when any of these holds:

| Condition | Meaning |
|---|---|
| `owner` is empty | its request ended without settling it (`unknown`) |
| `owner.instance_id` = this process's, but a different `boot_id` | **this process restarted**: its earlier life is certainly dead. Take over at once. |
| `lease_until` has passed | the owner overran its deadline + 30 s: presumed dead (e.g. another instance crashed) |

Taking over is one Firestore transaction (`take_over`). Exactly one process wins, and it becomes
the owner with a lease covering its recovery.

### 3.2 What triggers recovery (no background timer)

| Trigger | What runs |
|---|---|
| the service starts (prod mode) | one full reconcile, in the background: what the crashed boot left open is settled at once |
| `POST /reconcile` | full recovery (3.3) of every open payment nobody drives; the verification of flagged terminals (§5) |
| the POS repeats the same order | full recovery within that request's budget (1.9) |
| a cancel | a take-over of the purchase or undo it needs (2.2, 2.5) |
| a new payment meets the lock | one look at the blocker (§4); a PARTIAL left behind is released (1.8) |
| `GET` | **nothing**: reads never ask MarketPay |

### 3.3 `POST /reconcile`

- **Body** (optional): `{"terminalId": …, "olderThan": "<ISO time>"}`. It limits the run to one
  terminal and/or to payments created before that time.
- **Candidates:** every payment that holds an operation, or is `pending` / `unknown`, oldest
  first.
- **Per payment:**
  - no operation (a legacy record, or a partial left for a person) → **one look**. A finished
    record of this payment settles it, without touching any lock. A partial reversal is left alone.
  - a live owner → **skipped** (counted as still open);
  - orphaned → **take over and recover** (3.4). If that ends with a refund due but never sent, it
    is **released**: `approved` / `refund_due`, terminal freed, because recovery never starts a
    card transaction. A partial's reversal due but never sent is released as `unknown` /
    `partial_not_reversed`.
  - a store failure → counted as still open; the run continues.
- **Then:** the verification check for every flagged terminal (§5, non-final).
- **Answer:** `{scanned, resolved, stillOpen, resolvedIds}`. "Resolved" means it ended in a final
  state with the terminal free. Running it again is safe.

### 3.4 Recovering one operation (`Recovery._recover`)

**Never re-sends** anything that may have reached MarketPay.

| Operation | Recovery |
|---|---|
| **purchase** | Watch → abort → confirm (1.6), on the recovery window below. Still unclear → one more look, then the **time rule**: no record of this payment **180 s after it was created** → `failed` / `never_recorded` (flagged, §5). |
| **refund, sent** | The same, watching the refund's own reference, with the time rule counted from when it was sent. |
| **reversal, sent** | Watch for **this reversal's record** (2.3) until the window ends; then one more look. Still the purchase's record (or the baseline's) **180 s after it was sent** → it never landed: `approved` / `undo_not_recorded` (flagged, §5). Otherwise unclear: `unknown`, locked. |
| refund due, never sent | released (3.3), not started |
| partial reversal due, never sent | released (3.3), not started |

**The recovery window** (`recovery_window`):
- **Watch until** the moment the original request would itself have acted:
  - a purchase: its own abort point (`deadline_at` − 10 s);
  - a refund: sent + 50 s;
  - a reversal: sent + 180 s.
  It's never less than 5 s and never more than **90 s**.
- **Answer by:** 9 s after that, or, if later, just after the time rule could apply. Never more
  than **90 s** after starting.

Anything unresolved after 90 s converges on a later call. Why wait at all? After the service
crashes, an abort **can't** stop the terminal (the service's request died with the process), so
the terminal only ends the transaction on its own card timeout, about 120 s. Until then,
"not its own" proves nothing.

---

## 4. The one-look recheck

`application/recovery.py › Recovery.recheck`

Used by a cancel, by a POS retry, and on the blocker when a new payment meets the lock. It
applies only to a payment that holds an operation and is `unknown` or orphaned. A live request
is left alone.

| Operation | One look decides |
|---|---|
| purchase | **its own** transaction finished → its outcome. No record 180 s after creation → `failed` / `never_recorded`. Otherwise nothing changes. |
| reversal (sent) | this reversal's record → its status; nothing new 180 s after sending → `approved` / `undo_not_recorded`; otherwise nothing |
| refund (sent) | the refund's record → its outcome; the purchase still last 180 s after sending → not done; no record at all by then → not done |
| an undo due, never sent | nothing to look for |

---

## 5. Re-checking inferred outcomes

`domain/verification.py`

A late request, stuck in the network, could still reach the terminal after the service settled a
payment on "no record of it". These outcomes are **inferred**:

| Outcome | Inferred from |
|---|---|
| `failed` / `aborted`, via `abort_response` | a `204` abort + no record of this payment |
| `failed` / `never_recorded` | no record 180 s after sending |
| `approved` / `undo_not_recorded` | no reversal record 180 s after sending it |

A refund is re-checked the same way: one concluded only from a missing record is
`undo_not_recorded`, and it is looked for under **its own** reference (`rf01…`), not the purchase's.

1. When such a payment releases the terminal, the **terminal is flagged** with its id, in the
   same transaction.
2. **The next payment on that terminal** (1.5) takes one look **before sending**, then clears the
   flag. Its own transaction will overwrite the evidence, so this is the last chance.
3. **Reconcile** takes the same look but **keeps the flag if it finds nothing**, because the late
   request may still land.
4. What a look can find:

| Found | The old record becomes | Log |
|---|---|---|
| **its own transaction approved** for a payment the service reported `failed` | `approved` / `late_charge_found` (+ its `terminalTransactionId`) | `late_outcome_found` (error) |
| **this reversal's OK record** for one the service reported still charged | `cancelled`, `reversed: true` / `late_reversal_found` | `late_outcome_found` (error) |
| anything else | unchanged | `verification_found_nothing` |

**Nothing is reversed automatically.** A reversal needs a tap, and the terminal would show
"Refund" to the next customer. A person refunds a late charge.

---

## 6. Reads

- **`GET /payments/{id}`**: the stored record, as is. `404` if unknown. It never calls MarketPay.
- **`GET /payments`**: newest first (`createdAt`, then id); filters `state` (repeated or
  comma-separated), `terminalId`, `reference`, `createdAfter`/`createdBefore` (exclusive; a time
  with no zone is UTC); `limit` 1–500 (default 100); an opaque `cursor`.
  - Firestore only sorts by `createdAt` and applies the date range; state and terminal are
    filtered while scanning, and `reference` is a direct lookup, so no composite index is needed.
  - One request examines at most 2,000 documents. A short page can then still carry a
    `nextCursor`, and following it loses nothing.
  - A foreign or garbled cursor → `400`.

---

## 7. When Firestore fails

`application/store_policy.py › StorePolicy: run, change, record, note`

Every store call is retried on transient errors (`Aborted` after the library's own 5 attempts,
`ServiceUnavailable`, `DeadlineExceeded`, `InternalServerError`, `ResourceExhausted`, `Unknown`,
`Cancelled`), with backoff from 0.1 s, doubling, up to 1 s. Permanent errors (e.g.
`PermissionDenied`) aren't retried.

| The call is… | Retried until | If it still fails |
|---|---|---|
| **before anything reached MarketPay** (creating the payment, requesting a cancel, claiming an undo, taking over) | 5 s | `503 store_unavailable`: safe to repeat |
| **recording what MarketPay said** | the request's deadline | **return MarketPay's outcome anyway** (error log); the stored record keeps the lock and converges through reconcile |
| **a write-ahead note** ("abort requested", the reversal baseline) | 1 s | carry on (error log); only crash recovery loses a little precision |
| a re-read while waiting for another request | — | keep the last version seen, and try again |

A timed-out write may have landed. Retrying the same pure transition then sees its own result:
- creating the payment compares `created_by`;
- a claim or take-over that comes back "unchanged" but is owned by the retrying request counts as
  won.

---

## 8. Event sequences

The log events each scenario emits, in order, for one request. `http_request` (method, path,
status, duration) closes every request. Every event, with its level and fields:
[reference › Log events](reference.md#log-events).

**Happy path (the customer taps)**

| # | Event | Key fields |
|---|---|---|
| 1 | `payment_created` | amount, currency |
| 2 | `marketpay_call` | POST /process-transaction, status 201, duration |
| 3 | `transaction_answered` | outcome=completed |
| 4 | `payment_resolved` | state=approved, state_reason=bank_approved, resolved_via=process_response |

**Nobody taps: the abort while open**

| # | Event | Key fields |
|---|---|---|
| 1 | `payment_created` | |
| 2 | `marketpay_call` | POST /abort-transaction, **204** |
| 3 | `abort_sent` | attempt=1, outcome=aborted |
| 4 | `marketpay_call` | POST /process-transaction, 201 (the call that was held open) |
| 5 | `transaction_answered` | outcome=completed |
| 6 | `payment_resolved` | state=failed, state_reason=**aborted** |

**Tapped at the last moment: abort too late, the bank still answers in time**

`payment_created` → `marketpay_call` (abort, **409**) → `abort_sent` (outcome=too_late) →
`marketpay_call` (process, 201) → `transaction_answered` → `payment_resolved` (state=approved).

**Still open at the deadline, then a late answer**

`payment_created` → `abort_sent` (too_late or aborted) → `transaction_open_at_deadline` (warning)
→ `payment_resolved` (state=unknown, awaiting_result) → `http_request`. Later, on the worker
thread: `marketpay_call` (process) → `late_answer` → `late_answer_recorded`.

**The reply is lost (reset / timeout after sending)**

`payment_created` → `marketpay_unavailable` (warning) → `transaction_answered` (outcome=ambiguous)
→ [`marketpay_call` (last-transaction) → `last_transaction_polled` (sighting=…)] × n. Then either
the sighting is ours_finished → `payment_resolved`; or `polling_stopped` → `abort_sent` →
[`last_transaction_polled`] × n → `polling_stopped` → `abort_resolved` → `payment_resolved`.

**A request that never left the service (a DNS blip)**

`payment_created` → `marketpay_unavailable` → `transaction_not_sent_resending` (send=1) → … →
`marketpay_call` (process, 201) → `transaction_answered` → `payment_resolved`.

**Refused with `404` (terminal offline / wrong currency)**

`payment_created` → `marketpay_call` (process, 404) → `transaction_answered` (outcome=rejected) →
`marketpay_call` (last-transaction) [→ `rejection_look_failed` if the look fails, 2 attempts] →
`payment_resolved` (state=failed, provider_rejected).

**The terminal is busy**

[`payment_rechecked` (checked_reference=the blocker) if one look was allowed] → `terminal_busy`
(blocked_by) → `http_request` 409.

**The same order again**

`payment_duplicate_reference` (state) → [`payment_idempotency_mismatch` (fields) → 409]. While
another request drives it: no further events until it settles, then `http_request` 200.

**A POS reversal**

| # | Event | Key fields |
|---|---|---|
| 1 | `cancel_requested` | plan=undo, state=approved |
| 2 | `undo_started` | operation=reversal, attempt |
| 3 | `marketpay_call` | GET /last-transaction (the baseline) |
| 4 | *(if nobody taps)* `marketpay_call` + `abort_sent` | abort 204 at ~49 s |
| 5 | `marketpay_call` | POST /cancel-transaction, 200 |
| 6 | `reversal_sent` | outcome=cancel_completed |
| 7 | `cancel_finished` | kind=cancelled (or undo_failed), state, state_reason |

**A POS cancel of a running purchase**

`cancel_requested` (plan=stop_purchase) → `marketpay_call` (abort, 204) → `abort_sent`. On the
purchase's own request: `marketpay_call` (process, 201) → `transaction_answered` →
`payment_resolved` (state=cancelled, cancelled_before_charge). Then `cancel_finished`
(kind=cancelled).

**Reconcile after a crash**

`recovery_started` (operation, previous_owner) → [`last_transaction_polled`] × n →
[`polling_stopped` → `abort_sent` → … → `abort_resolved`] → (the time rule may apply) →
`reconcile_finished` (scanned, resolved, still_open). A live owner: `reconcile_skipped_live_owner`.
A flagged terminal: `verification_found_nothing`, or `late_outcome_found` (error).

**Firestore trouble**

`store_retry` (warning, what, attempt) × n → either success; or `store_unavailable` (error) →
`503`, before anything was sent. After MarketPay answered it's
`store_write_failed_returning_marketpay_outcome` (error), and the POS still gets MarketPay's
outcome.

---

## 9. MarketPay notifications (webhook)

`api/webhooks.py`, `application/notifications.py › NotificationIntake`, `domain/notifications.py`,
`application/notification_inbox.py`. **Built and tested (unit and API tests, with the real
notifications captured on staging); never received live by the service itself.**
It follows the integration guide (§3) and the three real notifications captured (approved,
and no card presented), which are also test fixtures (`observed_notification`,
`observed_timeout_notification`). Both came after a `202`; no progress notifications came.

Off unless `NOTIFICATION_BASE_URL` (public HTTPS) and `NOTIFICATION_SECRET` are set. Polling
stays the source every flow above relies on; a notification can only make one end sooner.

### 9.1 Sending

Every purchase, reversal and refund carries its own `ecrParams.notificationUrl`:
`{base}/webhooks/marketpay/{paymentId}/{operation}/{signature}`, the signature being an
HMAC-SHA256 of `paymentId:operation` under the secret. So a URL can't be guessed, nor reused
for another payment or operation, and nothing has to be stored. The logs mask the signature.

### 9.2 Receiving

1. Not a URL the app signed → `404`, logged `marketpay_notification_rejected` with the sender.
2. Otherwise → `200` **at once** (MarketPay never retries, and a slow endpoint may lose the delivery), logged `marketpay_notification` with the body (card data redacted) and the sender's address and header names. The rest runs on one worker thread, in arrival order.

⚠️ **Unauthenticated.** MarketPay doesn't sign the call: only the secret URL proves the sender. Before production add an IP allowlist of MarketPay's senders, or a signature/mTLS check if MarketPay offers one.

### 9.3 Using it (`accept_notification`)

| Notification | What happens | `notification_used` |
|---|---|---|
| Progress (`WAITING_FOR_CARD`, `PIN_REQUIRED`, `BANK_AUTHORIZATION`; per the guide; never seen on staging), or `COMPLETED` with no readable result | logged only | `progress` |
| For a payment the service doesn't have | logged only | `unknown_payment` |
| For an operation that is no longer running (settled, or moved on to its undo) | nothing changes; **error** `notification_contradicts_record` if it says "charged" and the service recorded "not charged", or the reverse — the record isn't flipped (the waiter may have re-run the card); the re-check of inferred outcomes (§5) and a person take it from there | `already_settled` |
| Not positively this operation's own record (another ecrTransactionId; a "reversal" carrying the purchase's own transaction id) | nothing changes | `not_conclusive` |
| Its own record, and **a request is driving the operation** | handed to that request via the in-process inbox: its poll takes it at once instead of waiting for last-transaction (which may lag or answer `500`). Never written behind the driver's back: that could free the terminal just as it sends an abort | `handed_to_driver` |
| Its own record, and **nobody is driving it** (`unknown`, orphaned) | recorded at once with the same transition a recheck would use (`apply_notified` → `apply_purchase` / `apply_undo`), in one Firestore transaction that re-checks nobody took it over; `resolved_via: notification` | `settled` |

A final notification becomes the record `last-transaction` would show
(`Notification.as_record`). The real ones carry `finalTransactionParams` (with numbers
where `last-transaction` has strings; both are read), and a `null` `terminalTransactionId` on
the envelope. The guide's example has no `finalTransactionParams`: then the envelope's
ecrTransactionId (and terminalTransactionId, if the result lacks it) is used. A result naming
another ecrTransactionId than the envelope is ignored. The record is then read by **the same observer
functions** polling uses (`observe_last_transaction`, `observe_reversal`), so the outcome is the
same whichever way it arrives: `status` wins over `responseCode` (although the guide says to
read `responseCode`), a contradiction is logged as `marketpay_result_inconsistent` with
`source: notification`.

### 9.4 Limits

- A poll accepts a notified record only if that record **alone** settles it. So a notification
  never counts as "no record of this payment", e.g. towards the two looks that conclude an abort.
- The inbox is in memory and per process: a notification that reaches another instance, or
  arrives across a restart, is simply not used; polling or reconcile settle the payment.
- The spec promises the final result only after a `202` (both captured ones followed one),
  but staging sends it after a `201` too. So on the normal path it arrives just after the
  HTTP answer: `already_settled`, a free cross-check. It matters where the request broke (a lost
  `201`, a reset, a `5xx`; after the service crashed: unconfirmed) and for `unknown` payments.
