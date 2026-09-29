# Reference

Everything the service uses, named and explained: states, reasons, stored fields, the terminal
lock, every timeout, configuration, the MarketPay calls, and every log event. The behaviour
itself is in [flows.md](flows.md).

- [Payment states](#payment-states)
- [State reasons](#state-reasons)
- [Stored payment fields](#stored-payment-fields)
- [Terminal lock](#terminal-lock)
- [Firestore data model](#firestore-data-model)
- [Timeouts and limits](#timeouts-and-limits)
- [Configuration](#configuration)
- [MarketPay calls](#marketpay-calls)
- [HTTP API status codes](#http-api-status-codes)
- [Log events](#log-events)

---

## Payment states

The `state` field of the API's `Payment` (contract enum). **Final** states are backed by a
MarketPay record; only they, together with nothing left in flight, free the terminal.

| State | Final | Meaning for the waiter |
|---|---|---|
| `pending` | no | A request is running it right now (seen by other readers, not by the POS that created it). |
| `approved` | yes | Charged, and the charge stands. |
| `declined` | yes | Not charged: the bank declined, or a partial approval was reversed. Ask for another card. |
| `failed` | yes | Not charged: it stopped before the bank (Cancel pressed, no card, the service's abort), was refused, or never ran. Safe to try again with a new reference. |
| `cancelled` | yes | Not charged any more: stopped on a POS cancel, or reversed/refunded (`reversed: true`). |
| `unknown` | no | **Not yet known.** Don't re-run the card. It settles through `POST /reconcile`, a retry of the same order, or the terminal's next payment. With `partial_not_reversed` / `partial_reversal` it needs a person. |

---

## State reasons

`state_reason` is internal (stored and logged, not in the API response). It explains **why** a
payment is in its state. These are the service's labels; MarketPay's own codes go in
`declineReason`.

| Reason | With state | Set when |
|---|---|---|
| `bank_approved` | approved | `status OK` |
| `bank_declined` | declined | `status NOK` **with** an acquirer `responseCode` (kept as `declineReason`) |
| `terminal_stopped` | failed | `status NOK` **without** a code: stopped on the terminal before the bank (on staging, Cancel pressed and no card in time look identical) |
| `aborted` | failed | stopped by **the service's** abort (`204`): either the terminal's NOK record, or a `204` plus no record of this payment (inferred) |
| `not_sent` | failed | the connection failed before the request left the service, and re-sending for 15 s didn't help |
| `provider_rejected` | failed | `400`/`404` from `process-transaction`, and a look found no record of this payment |
| `never_recorded` | failed | no record of this payment 180 s after sending (inferred) |
| `cancelled_before_charge` | cancelled | stopped on the terminal after a POS cancel |
| `reversed` | cancelled | `cancel-transaction` OK |
| `refunded` | cancelled | a REFUND transaction approved |
| `partial_approval_reversed` | declined | a `PARTIAL` approval, reversed |
| `awaiting_result` | unknown | a purchase with no answer yet (202 / lost reply / open at the deadline) |
| `awaiting_undo` | unknown | a reversal/refund with no visible outcome yet |
| `partial_approval` | unknown | a `PARTIAL`, with its reversal about to run |
| `not_our_transaction` | unknown | a result echoing another `ecrTransactionId` |
| `unrecognised_result` | unknown | a result without `status` |
| `partial_reversal` | unknown | `cancel-transaction` answered `PARTIAL`: **needs a person** |
| `partial_not_reversed` | unknown | a `PARTIAL` that couldn't be reversed: **needs a person** |
| `undo_refused` | approved | a reversal/refund definitively not done (NOK, 4xx); the charge stands |
| `undo_not_sent` | approved | a reversal/refund never reached MarketPay (or a repeated reversal was held back: its baseline wasn't known and stored); the charge stands |
| `undo_not_recorded` | approved | no reversal/refund record 180 s after sending, or a refund stopped by the service's abort with no record (inferred, so re-checked); the charge stands |
| `refund_due` | approved | a refund is due (approved despite a cancel), but recovery doesn't start card transactions: the POS must cancel again |
| `late_charge_found` | approved | the service had reported `failed` on an inference; the terminal later showed it approved ([flows §5](flows.md#5-re-checking-inferred-outcomes)). **Refund manually.** |
| `late_reversal_found` | cancelled | the service had reported the charge standing; the reversal did land (flows §5) |

`resolved_via` (internal) says which MarketPay answer the state rests on: `process_response`,
`cancel_response`, `last_transaction`, `abort_response` (an inference from the abort's `204`), or
`notification` (MarketPay's notification webhook settled it, flows §9).

---

## Stored payment fields

`payments/{id}` in Firestore (`domain/models.py › Payment`). The API returns the first block
only.

| Field | Meaning |
|---|---|
| `id` | UUIDv5 of `reference`: the same order is always the same document |
| `terminal_id`, `amount` (öre), `currency` (alpha), `reference` | the order; `reference` is sent as `ecrTransactionId` |
| `state`, `reversed` | see [Payment states](#payment-states) |
| `provider_transaction_id` | MarketPay's `terminalTransactionId` of the purchase (needed for a reversal) |
| `decline_reason` | MarketPay's `responseCode` verbatim, or null (the service never invents one) |
| `created_at`, `updated_at` | Firestore timestamps |
| `state_reason`, `state_detail`, `resolved_via` | diagnostics: why, and on which answer |
| `operation` | `purchase` / `reversal` / `refund` while one is in flight. **Set exactly while the payment holds the terminal lock.** |
| `owner` {`instance_id`, `boot_id`} | the process driving `operation`; empty = nobody (orphaned) |
| `lease_until` | after this, the owner is presumed dead (the request's deadline + 30 s) |
| `deadline_at` | when the POS stops waiting for the purchase (recovery waits until then) |
| `created_by` | a random id of the request that created it (tells a lost-commit retry from a duplicate) |
| `abort_requested_at` | the service asked the terminal to abort (written before sending, and again with the outcome if that note was lost). An approval after this is undone by REFUND. |
| `cancel_requested_at` | the POS asked to cancel |
| `undo_started_at`, `undo_attempts` | a reversal/refund was claimed (and maybe sent); how many times |
| `undo_reason` | `pos_cancel` → ends `cancelled`; `partial_approval` → ends `declined` |
| `undo_baseline_transaction_id` | the terminal's last record just before the reversal was sent (a reversal reuses the payment's own `ecrTransactionId`; only a new `terminalTransactionId` tells it apart) |
| `refund_reference` | `ecrTransactionId` of the latest refund attempt (`rf01…`, `rf02…`); cleared by a later reversal attempt, so it says which kind the latest undo was. Always 36 characters for attempts 1–99 (MarketPay's maximum); a 100th refund attempt would exceed it |
| `claim_id` | a random token of the request whose claim (a take-over, an undo) holds `operation`: tells two requests of one process apart |
| `history_length` | how many entries the payment's history has ([README › Payment history](../README.md#payment-history)) |

---

## Terminal lock

`terminals/{terminalId}` holds `lock` {`payment_id`, `reference`, `locked_at`} or null, and
`verify_payment_id` (§5 of flows).

**Rule:** a payment holds the lock **exactly while its `operation` is set**. Every store
transaction that changes a payment also decides what happens to the lock (`lock_action`):

| The new version… | …and the lock is | Action |
|---|---|---|
| has an operation | free | **acquire** (in the same transaction) |
| has an operation | held by this payment | keep |
| has an operation | held by **another** payment | **busy**: nothing is written; the caller answers `409 terminal_busy` |
| has no operation | held by this payment | **release** (and flag the terminal if the outcome was inferred, flows §5) |
| has no operation | free / someone else's | keep (a payment never touches another's lock) |

Consequences:
- a terminal runs **one** operation at a time;
- `last-transaction` can't be overwritten while an outcome is unread;
- an `unknown` purchase keeps its terminal until it's settled.

`GET /terminals` shows each terminal as `locked` / `lockedBy`.

---

## Firestore data model

| Collection | Document id | Written by |
|---|---|---|
| `payments` | UUIDv5(`reference`) | every payment transition (one transaction each) |
| `payments/{id}/history` | `0001_<time of the change>`, sortable | in the same transaction as each change of state, reason or terminal hold ([README › Payment history](../README.md#payment-history)) |
| `terminals` | the terminal id (`PAX:TEST_TERMINAL`) | together with the payment transitions that take or free a lock, and when a verification flag is cleared |
| `_health` | `ping` (read only) | `GET /readyz` reads it to prove credentials and database |

Queries (none needs a composite index):
- open payments, by `state in […]` or `operation in […]`, optionally with `terminal_id ==`;
- the list, ordered by `created_at` with a range;
- flagged terminals, `verify_payment_id > ""`;
- a payment's history, ordered by `number`.

A history entry holds `number`, `at`, `state`, `reason`, `based_on` (which MarketPay answer the
state rests on), `operation` (what holds the terminal; `null` = released), `detail`, and a
one-line `summary`. The payment's `history_length` counts its entries.

---

## Timeouts and limits

| Name | Value | Where | Why |
|---|---|---|---|
| deadline (`deadlineSeconds`) | 1–120, default 60 | request | how long the POS waits for a definitive answer |
| `waitTime` | min(300, D + 90) | `domain/budget.py` | keeps the service's request open past the deadline, so an abort still works; covers the ~120 s card timeout |
| read timeout (payment/cancel call) | `waitTime` + 5 s | `domain/budget.py` | the service always hears MarketPay's answer |
| abort point | max(D − 10, D/2), at most answer by − m | `domain/budget.py` | 10 s to abort and hear the result; halfway for short deadlines; a third for 3 s or less |
| answer by | D − m, m = min(1 s, D/3) | `domain/budget.py` | 1 s to record and respond; two thirds of a 3 s-or-less deadline |
| poll interval | 1 s | `domain/budget.py` | looks while waiting for a record |
| one look / one abort | ≤ 5 s each | `domain/budget.py` | bounded calls |
| abort attempts | 3 (only while there's no usable answer) | `domain/abort.py` | |
| "not this payment's" to conclude after a `204` | 2 consecutive looks | `domain/abort.py` | the record appears ~1 s after a 204 |
| re-send what never left | backoff 0.5 → 4 s, ≤ 15 s, never past the abort point | `application/terminal_ops.py` | absorbs a DNS blip; fails fast on a bad certificate |
| confirming a `400`/`404` | 2 looks, 1 s apart | `application/terminal_ops.py` | catches a network-duplicated copy that ran |
| cancel phase | 59 s (60 − 1); abort point 49 s | `domain/cancel.py` | a reversal waits for a tap like a payment |
| reversal sends (never left the service) | at most 3 in all (2 re-sends), 1 s apart | `domain/cancel.py` | |
| baseline look before a reversal | ≤ 3 attempts, 1 s apart | `application/terminal_ops.py` | |
| re-read while waiting for another request | every 0.5 s | `domain/cancel.py` | duplicates, concurrent cancels |
| lease | the request's deadline + 30 s | `domain/recovery.py` | past this, its owner is presumed dead |
| **longest unrecorded transaction** (time rule) | **180 s** | `domain/recovery.py` | card timeout ~120 s + margin; beyond it "no record" means it never ran (an assumption to confirm with MarketPay) |
| reversal/refund "never landed" | 180 s | `domain/recovery.py` | a reversal waits for a tap |
| recovery: watch at least / at most | 5 s / 90 s per payment | `domain/recovery.py` | reconcile answers an HTTP request |
| store retries | backoff 0.1 → 1 s; 5 s before sending, until the deadline after MarketPay answered | `application/store_policy.py` | nothing charged yet → fail fast; charged → never lose MarketPay's answer |
| Firestore call timeout | 10 s | `infrastructure/store/firestore.py` | |
| list: page size / scan limit | 1–500 (default 100) / 2,000 documents | `domain/listing.py`, `application/queries.py` | no composite index needed |
| MarketPay connect timeout | 5 s | config | |
| worker threads for held-open calls | 64 | `application/terminal_ops.py` | one per payment in flight |
| notified records kept for polls | 10 min, the last 8 per terminal | `application/notification_inbox.py` | a poll still running can pick a notification up |

---

## Configuration

All from environment variables (`config.py`); `docker compose` reads `.env` (copy
`.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `MARKETPAY_BASE_URL` | — | e.g. `https://marketpay.example.test` |
| `MARKETPAY_STORE_CODE` | — | sent with `GET /terminals` |
| `MARKETPAY_ECR_ID` | — | sent as `ecrParams.ecrId` |
| `MARKETPAY_CLIENT_CERT`, `MARKETPAY_CLIENT_KEY` | — | mTLS certificate and key paths (mounted read-only) |
| `MARKETPAY_USER_AGENT` | `TestAssignment/1.0` | must be non-empty (an empty one gets an empty `400`) |
| `MARKETPAY_CONNECT_TIMEOUT_SECONDS` | 5 | |
| `MARKETPAY_READ_TIMEOUT_SECONDS` | 10 | for non-payment calls (`/terminals`) |
| `MARKETPAY_CURRENCY`, `MARKETPAY_TERMINAL_ID` | `752`, — | informational (the request carries both) |
| `GOOGLE_CLOUD_PROJECT` | — | the Firestore project |
| `FIRESTORE_DATABASE` | `(default)` | |
| `GOOGLE_APPLICATION_CREDENTIALS` | set by compose | the service-account key (mounted read-only) |
| `FIRESTORE_EMULATOR_HOST` | unset | point at an emulator instead |
| `INSTANCE_ID` | `default` | **must be unique per running process** and stable across its restarts (a deployment rule; the shipped entrypoint runs one process) |
| `NOTIFICATION_BASE_URL` | unset | public HTTPS base for MarketPay's notifications (the bonus, flows §9); off unless set together with the secret |
| `NOTIFICATION_SECRET` | unset | ≥ 32 characters; signs each notification URL, the **only** proof a notification is MarketPay's |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `console` for humans |
| `RECONCILE_ON_START` | `false`; `true` in prod mode (the entrypoint) | one reconcile when the service starts, so a restart converges by itself (not a timer) |
| `APP_MODE`, `PORT` | `prod`, 8080 | `prod` = gunicorn, 1 worker, 32 threads, no reloader; `dev` = Flask with live reload (development only) |

---

## MarketPay calls

All go through `infrastructure/marketpay/client.py` (the `MarketPayGateway` port), with mTLS and a `User-Agent`. None of them raises for
network trouble; each returns a typed outcome.

| Call | When | Timeout | Outcomes |
|---|---|---|---|
| `POST /process-transaction` (PURCHASE / REFUND) | take a payment; a refund | read = `waitTime` + 5 s | `Completed` (201) · `Accepted` (202) · `Rejected` (4xx) · `Ambiguous` (read timeout, reset after sending, 5xx, unreadable 201) · `NotSent` (connect-phase failure) |
| `POST /cancel-transaction` | a reversal | read = `waitTime` + 5 s | `CancelCompleted` (200) · `Accepted` · `Rejected` · `Ambiguous` · `NotSent` |
| `POST /abort-transaction` | at the abort point; a POS cancel of a running purchase; recovery | ≤ 5 s | `Aborted` (204) · `TooLate` (409) · `AbortRefused` (other 4xx) · `AbortUnconfirmed` (network, 5xx) |
| `GET /last-transaction` | only off the happy path | ≤ 5 s | `Found` · `LookupFailed`. **One at a time per terminal** (overlapping calls get `500`); callers queue. |
| `GET /terminals` | diagnostics | 10 s | the list, or `502` |

**Connect-phase failures** (`NotSent`) are `ConnectError`, `ConnectTimeout` and `PoolTimeout`: no
byte of the request reached MarketPay. Everything after that is `Ambiguous`.

**Result contradictions** are logged once per record as `marketpay_result_inconsistent`:
- `NOK` with code `000` (`status` wins: `declined`);
- `OK` without code `000` (stays `approved`).

---

## HTTP API status codes

Contract codes plus the service's own; details in [api-extensions.md](api-extensions.md).

| Endpoint | Codes |
|---|---|
| `POST /payments` | 201, 200, 400 `validation_error`, 409 `terminal_busy`, 409 `idempotency_mismatch`, 503 `store_unavailable` |
| `GET /payments/{id}` | 200, 404 `not_found`, 503 |
| `GET /payments` | 200, 400 `validation_error` (incl. a bad cursor), 503 |
| `POST /payments/{id}/cancel` | 200, 404, 409 `not_cancellable` / `cancel_failed` / `needs_attention` / `terminal_busy`, 503 |
| `POST /reconcile` | 200, 400, 503 |
| `GET /payments/{id}/history` | 200, 404 `not_found`, 503 |
| `POST /webhooks/marketpay/{paymentId}/{operation}/{signature}` | 200, 404 (a URL the service didn't sign) |
| `GET /healthz` / `GET /readyz` | 200 / 200 or 503 |
| `GET /terminals`, `GET /terminals/{id}/last-transaction` | 200, 502 `provider_unavailable` / `provider_error` |

Every error body is `{"code", "message"}`, except the health checks: `/readyz` answers `503`
with `{"status": "unavailable", "firestore": …}`.

---

## Log events

structlog: JSON by default (`LOG_FORMAT=console` for humans). Every line of a request carries
`request_id`, and payment events carry `payment_id`, `reference` and `terminal_id`. Events about
*another* payment use `checked_*` or `verified_payment_id`.

| Event | Level | Meaning (key fields) |
|---|---|---|
| `app_started` | info | process up (`instance_id`, `boot_id`) |
| `marketpay_notification` | info | a signed notification arrived (status, ecr / terminal transaction id, body without card data, sender address, header names) |
| `marketpay_notification_rejected` | warning | a webhook call with a URL the service never signed → `404` (sender details) |
| `notification_used` | info | what it was used for: `progress`, `unknown_payment`, `already_settled`, `not_conclusive`, `handed_to_driver`, `settled` |
| `settled_by_notification` | info | a poll ended on a notified record instead of last-transaction |
| `payment_settled_by_notification` | info | an operation nobody drove was settled from its notification |
| `notification_contradicts_record` | error | a notification for a settled payment disagrees about whether it was charged (recorded vs notified state) |
| `notification_processing_failed` | error | the worker failed; the notification is lost, polling settles the payment |
| `http_request` | info | a request finished (method, path, status, `duration_ms`) |
| `unhandled_error` | error | a bug: answered `500` |
| `payment_created` | info | intent stored, terminal locked |
| `payment_duplicate_reference` | info | the same reference again (the existing state) |
| `payment_idempotency_mismatch` | info | the same reference, a different order (fields) → 409 |
| `terminal_busy` | info | a new payment refused (`blocked_by`) |
| `transaction_answered` | info | the payment/refund call returned (outcome kind) |
| `transaction_not_sent_resending` | warning | never left the service; re-sending (send, reason) |
| `transaction_open_at_deadline` | warning | no answer by the deadline; answering `unknown` (abort kind) |
| `abort_sent` | info | abort attempt and its outcome |
| `abort_resolved` | info | the outcome decided after an abort (abort, state) |
| `last_transaction_polled` | info | one look while watching (poll, sighting) |
| `last_transaction_lookup_failed` | warning | a look failed while watching (reason) |
| `polling_stopped` | info | watching ended (polls) |
| `lookup_failed` | warning | a one-off look failed (reason) |
| `rejection_look_failed` | warning | the look confirming a 400/404 failed |
| `rejected_but_ours_finished` | warning | a 400/404, but a copy of this payment's request had run |
| `baseline_lookup_failed` | warning | the look before a reversal failed |
| `payment_resolved` | info | the outcome recorded (state, operation, owner, state_reason, state_detail, resolved_via) |
| `payment_rechecked` | info | one look settled a payment (`checked_payment_id`, `checked_reference`, …) |
| `late_answer` | info | a held-open call answered after the service had replied |
| `late_answer_recorded` | info | …and it was recorded |
| `late_answer_failed` / `late_answer_not_recorded` | error | …and it couldn't be (a later look settles it) |
| `partial_reversal_not_started` | error | a PARTIAL's reversal couldn't be claimed (store down) |
| `cancel_requested` | info | a POS cancel (plan, state) |
| `undo_started` | info | a reversal/refund claimed (operation, attempt) |
| `reversal_sent` | info | `cancel-transaction` returned (attempt, outcome) |
| `reversal_open_at_deadline` | warning | a reversal still open at the cancel deadline (abort) |
| `cancel_finished` | info | the cancel's result (kind, state, …) |
| `recovery_started` | info | an orphaned operation taken over (operation, `previous_owner`) |
| `reconcile_skipped_live_owner` | info | not orphaned: left alone |
| `reconcile_payment_skipped` | error | the store failed for one payment; counted as still open |
| `reconcile_finished` | info | scanned, resolved, still_open |
| `late_outcome_found` | **error** | a charge/reversal turned up after the service reported otherwise (was, now) |
| `verification_found_nothing` | info | inferred-outcome re-check: nothing changed |
| `verification_skipped` | warning | inferred-outcome re-check: no answer from MarketPay |
| `verification_failed` | error | inferred-outcome re-check: the store failed |
| `marketpay_call` | info | every MarketPay call that got an HTTP answer (method, path, status, `duration_ms`) |
| `marketpay_unavailable` | warning | no HTTP answer (error type, detail) |
| `marketpay_result_inconsistent` | **error** | status vs responseCode contradiction, with details |
| `store_retry` | warning | a transient Firestore error, retrying (what, attempt) |
| `store_unavailable` | error | Firestore gave up (what, attempts) |
| `store_write_failed_returning_marketpay_outcome` | **error** | recorded nothing, but answered MarketPay's outcome |
| `store_note_failed` | error | a write-ahead note was lost |
| `store_read_failed_while_waiting` | warning | a re-read failed; kept the last version |
| `firestore_unreachable` | warning | `GET /readyz` failed |
| `late_answer_not_recorded` | error | an answer that came after the request ended couldn't be written; a later look settles it |
| `reversal_not_sent` | warning | a repeated reversal without a stored baseline was not sent (its record couldn't be told from the earlier attempt's) |
| `partial_left_to_a_person` | warning | a PARTIAL whose reversal nobody will send was released for another payment; a person settles it |
| `startup_reconcile_finished` | info | the one reconcile at start is done (scanned, resolved, still_open) |
| `startup_reconcile_failed` | error | the reconcile at start failed; `POST /reconcile` still works |
| `notification_for_unknown_payment` | warning | a signed notification for a payment the service doesn't have |
| `notification_after_settlement` | info | a notification for an operation that has already settled (the normal case after a `201`) |
| `notification_not_conclusive` | warning | not positively this operation's own record: nothing changes |
| `notification_handed_to_driver` | info | a live request drives the operation: the record goes to its poll |
| `notification_result_unreadable` | warning | a `COMPLETED` notification whose `result` the service can't read |
| `notification_result_contradictory` | warning | the result names another `ecrTransactionId` than the notification |
