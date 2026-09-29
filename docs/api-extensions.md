# API beyond the contract

What the service exposes or returns beyond `payment-api.yaml` (the contract the service
implements). None of it changes the contract's own endpoints. It all exists for diagnostics
and operations; the README links here.

## Extra endpoints

All read-only, except the webhook.

| Endpoint | Purpose | Notes |
|---|---|---|
| `GET /healthz` | Liveness: the process is up. | No dependencies checked. |
| `GET /readyz` | Readiness: one round trip to Firestore. | `503` if Firestore can't be reached. |
| `GET /payments/{id}/history` | The payment's timeline, oldest first: one entry per change of its state, reason or terminal hold, each with a readable `summary` ([README › Payment history](../README.md#payment-history)). | Read-only; stored state only. `404` for an unknown payment. |
| `GET /terminals?connected=` | MarketPay's terminal list, **with the service's lock state added** (below). | Read-only. |
| `GET /terminals/{terminalId}/last-transaction` | What MarketPay reports as the terminal's latest transaction, parsed by the service's models. | Read-only. Unknown fields are dropped, so receipts don't appear. |
| `POST /webhooks/marketpay/{paymentId}/{operation}/{signature}` | MarketPay's notifications (the bonus). Only on when `NOTIFICATION_BASE_URL` and `NOTIFICATION_SECRET` are set. | For MarketPay, not the POS. Answers `200` at once and uses the notification on a worker thread ([flows §9](flows.md#9-marketpay-notifications-webhook)): a final result settles the payment as last-transaction would. A URL whose HMAC signature doesn't match gets `404`. ⚠️ **Unauthenticated apart from the secret URL** ([open questions](open-questions.md#for-marketpay)): add an IP allowlist or a MarketPay signature check before trusting it. |

## `GET /terminals`: response differs from MarketPay's

MarketPay's `GET /terminals` (api.json, `TerminalSession`) returns a **bare array** of:

```json
{ "terminalId": "PAX:TEST_TERMINAL", "wsCreatedTime": "2026-09-27T01:31:14.498Z", "connected": true }
```

The service's response differs in two ways:

1. **Wrapped in `{"items": [...]}`**, the same list shape as the service's `PaymentList`.
2. **Two fields added to each terminal**, from the service's own terminal lock:
   - `locked`: `true` while a payment on this terminal has an operation in flight (purchase, reversal or refund) whose outcome isn't confirmed yet. A new payment on it gets `409 terminal_busy`.
   - `lockedBy`: which payment holds it, as `{ "paymentId", "reference", "lockedAt" }`, or `null`. Use it to decide which payment to look at or reconcile.

```json
{
  "items": [
    {
      "terminalId": "PAX:TEST_TERMINAL",
      "wsCreatedTime": "2026-09-27T01:31:14.498084Z",
      "connected": true,
      "locked": true,
      "lockedBy": {
        "paymentId": "f2e47bbc-0ee9-5999-bac1-f7ee1773179c",
        "reference": "order-in-progress",
        "lockedAt": "2026-09-27T14:52:33.173317Z"
      }
    }
  ]
}
```

**Why the service marks rather than hides a locked terminal.** Hiding it from the list wouldn't
stop anyone, because the POS doesn't have to consult the list before paying; the lock is enforced
in `POST /payments`. A terminal that silently disappears is also harder to operate than one
marked with the payment that holds the lock.

## How the service reads the contract's `GET /payments`

The contract lists the parameters but leaves their exact meaning open. The service's reading:

- **Order:** newest first (`createdAt` descending). Payments created in the same instant are
  ordered by id, so paging is deterministic.
- **`state`:** repeat it (`?state=approved&state=declined`) or comma-separate it
  (`?state=approved,declined`).
- **`createdAfter` / `createdBefore`:** **exclusive** bounds, ISO 8601. A time without a zone
  counts as UTC.
- **`limit`:** 1–500, default 100. **`cursor`:** opaque; pass back the `nextCursor` you received.
  A cursor the service didn't issue → `400`.
- **Stored state only**, like `GET /payments/{id}`: reads never ask MarketPay, so they're
  fast and give a stable snapshot. Unresolved payments converge through `POST /reconcile`.
- **A page can be shorter than `limit`** and still have a `nextCursor`, when a sparse filter makes
  the scan stop early (no composite index is needed, see [open questions › Decisions](open-questions.md#decisions-made-recorded-here-with-the-trade-off)). Follow `nextCursor` until it's `null`; a full last page ends with
  `null`, not with an empty extra page.

## Status codes beyond the contract

`payment-api.yaml` lists 400/404/409 (`terminal_busy`) for `POST /payments` and 404/409 for
cancel. The service returns these as well; every error body keeps the contract's `Error` shape
(`{code, message}`), so a client can branch on `code`. (The health checks are the exception:
`/readyz` answers `503` with `{"status": "unavailable", "firestore": …}`.)

| Endpoint | Status | `code` | When |
|---|---|---|---|
| `POST /payments` | 409 | `idempotency_mismatch` | The reference already names a payment for a different terminal, amount or currency: one reference is one order. |
| `POST /payments` and others | 503 | `store_unavailable` | Firestore is down **before** anything was sent to MarketPay. Safe to repeat. |
| `POST /payments` | 503 | `store_unavailable` | The previous payment on the terminal turned out charged after all, and its correction couldn't be stored: this payment isn't sent (it would overwrite the evidence). Nothing charged; safe to repeat. |
| `POST /reconcile` | 400 | `validation_error` | The body isn't a JSON object, or a field is malformed. |
| any | 500 | `internal_error` | An unexpected error (a bug): logged with its traceback. Never for a charge MarketPay made: that answer is returned even if the store fails. |
| `POST /payments/{id}/cancel` | 409 | `not_cancellable` | Declined or failed: nothing was charged. |
| `POST /payments/{id}/cancel` | 409 | `cancel_failed` | The reversal/refund definitively didn't happen; the payment is still approved. |
| `POST /payments/{id}/cancel` | 409 | `needs_attention` | E.g. a partial approval or a partial reversal that must be settled by a person. |
| `POST /payments/{id}/cancel` | 409 | `terminal_busy` | Another payment holds the terminal; a reversal can't run now. |
| `GET /terminals`, `GET /terminals/{id}/last-transaction` | 502 | `provider_unavailable` / `provider_error` | MarketPay gave no usable answer. |

## Input validation stricter than the contract

The contract's `CreatePayment` is looser than what MarketPay (or Firestore) accepts; the service
refuses early with `400 validation_error` instead of failing later:

- **`terminalId`** must be `MANUFACTURER:serial` with an uppercase manufacturer, no `/`, no
  spaces, at most 128 characters (`^[A-Z0-9]+:[^/\s]+$`). MarketPay answers `404` to a lowercase
  manufacturer, and the id is also the service's Firestore document id for the terminal lock.
- **`currency`** must be one the service can map to MarketPay's numeric code: `SEK`, `EUR`, `DKK`,
  `NOK`. (The terminal approves SEK only; MarketPay refuses the others with `404`.)
- **`reference`** is 1–36 characters: it is MarketPay's `ecrTransactionId`, whose maximum is 36.
- **`amount`** and **`deadlineSeconds`** are read leniently: a whole number written as `1299.0`
  or `"1299"` is accepted as `1299`.

## Fields stored but not returned

Payments carry internal fields that are stored in Firestore and logged, but are **not** part
of the `Payment` response, so the contract's shape stays exact:

- **Diagnostics:** `state_reason`, `state_detail`, `resolved_via`.
- **Coordination:** `operation`, `owner`, `claim_id`, `lease_until`, `deadline_at`,
  `created_by`, the abort/cancel/undo timestamps, `undo_attempts`,
  `undo_baseline_transaction_id` and `refund_reference`.
- **History:** `history_length`, the count of the payment's history entries (served by
  `GET /payments/{id}/history`).
