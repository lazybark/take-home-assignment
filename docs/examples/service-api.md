# The service's API: request and response examples

Every example below was produced by **running the real service code** (`scripts/generate_api_examples.py`): the Flask handlers, the service and the store rules, against a scripted MarketPay and an in-memory store, with the clock fixed at 2026-09-27 12:00 UTC. Nothing is hand-written, but potentially sensitive data is replaced by placeholders. Ids are deterministic examples (UUIDv5 of the reference); timestamps are the fixed clock. Regenerate after changing the API:

```bash
docker compose run --rm --no-deps api uv run python scripts/generate_api_examples.py
```

Contract: [`payment-api.yaml`](../payment-api.yaml). Beyond it: [api-extensions.md](../api-extensions.md). What MarketPay really sends: [marketpay-api.md](marketpay-api.md).

- [POST /payments](#post-payments--take-a-payment) (12 examples)
- [GET /payments/{id} and GET /payments](#get-paymentsid-and-get-payments--reads) (7 examples)
- [POST /payments/{id}/cancel](#post-paymentsidcancel--cancel--reverse) (8 examples)
- [POST /reconcile](#post-reconcile--recover-open-payments) (4 examples)
- [Diagnostics (beyond the contract)](#diagnostics-beyond-the-contract) (6 examples)

---

## POST /payments — take a payment

Synchronous: the response is the payment in its resolved state. See flows.md §1.

### Approved (the happy path)

*MarketPay (scripted):* process-transaction → 201, status OK, responseCode 000

**Request**

```http
POST /payments
Content-Type: application/json

{
  "terminalId": "PAX:<TERMINAL_ID>",
  "amount": 1299,
  "currency": "SEK",
  "reference": "order-1001"
}
```

**Response** `201`

```json
{
  "amount": 1299,
  "createdAt": "2026-09-27T12:00:00Z",
  "currency": "SEK",
  "declineReason": null,
  "id": "7b1e485a-9331-5817-87e5-60cc5d9023d4",
  "providerTransactionId": "14",
  "reference": "order-1001",
  "reversed": false,
  "state": "approved",
  "terminalId": "PAX:<TERMINAL_ID>",
  "updatedAt": "2026-09-27T12:00:00Z"
}
```

### Declined by the bank

*MarketPay (scripted):* 201, status NOK, responseCode 116 (kept as declineReason)

**Request**

```http
POST /payments
Content-Type: application/json

{
  "terminalId": "PAX:<TERMINAL_ID>",
  "amount": 1299,
  "currency": "SEK",
  "reference": "order-1002"
}
```

**Response** `201`

```json
{
  "amount": 1299,
  "createdAt": "2026-09-27T12:00:00Z",
  "currency": "SEK",
  "declineReason": "116",
  "id": "608ff88f-7931-5800-9b5a-1d214b1bdc57",
  "providerTransactionId": "14",
  "reference": "order-1002",
  "reversed": false,
  "state": "declined",
  "terminalId": "PAX:<TERMINAL_ID>",
  "updatedAt": "2026-09-27T12:00:00Z"
}
```

### Failed: stopped on the terminal (Cancel pressed)

NOK without an acquirer code means it never reached the bank: `failed`, not `declined`.

*MarketPay (scripted):* 201, status NOK, no responseCode

**Request**

```http
POST /payments
Content-Type: application/json

{
  "terminalId": "PAX:<TERMINAL_ID>",
  "amount": 1299,
  "currency": "SEK",
  "reference": "order-1003"
}
```

**Response** `201`

```json
{
  "amount": 1299,
  "createdAt": "2026-09-27T12:00:00Z",
  "currency": "SEK",
  "declineReason": null,
  "id": "e10dc195-6ce1-50cb-97e2-9fee48d3a306",
  "providerTransactionId": "14",
  "reference": "order-1003",
  "reversed": false,
  "state": "failed",
  "terminalId": "PAX:<TERMINAL_ID>",
  "updatedAt": "2026-09-27T12:00:00Z"
}
```

### Failed: nobody tapped, aborted at the deadline

The abort is sent at 10 s while MarketPay still holds the request open; the open request returns the NOK.

*MarketPay (scripted):* process-transaction held open → abort-transaction 204 → the open request returns 201 NOK

**Request**

```http
POST /payments
Content-Type: application/json

{
  "terminalId": "PAX:<TERMINAL_ID>",
  "amount": 1299,
  "currency": "SEK",
  "reference": "order-1004",
  "deadlineSeconds": 20
}
```

**Response** `201`

```json
{
  "amount": 1299,
  "createdAt": "2026-09-27T12:00:00Z",
  "currency": "SEK",
  "declineReason": null,
  "id": "d1264871-ca21-5725-a542-ca94942e1b61",
  "providerTransactionId": "14",
  "reference": "order-1004",
  "reversed": false,
  "state": "failed",
  "terminalId": "PAX:<TERMINAL_ID>",
  "updatedAt": "2026-09-27T12:00:10Z"
}
```

### Unknown: the bank hadn't answered by the deadline (rare)

Honest `unknown` instead of a guess. The terminal stays locked; the late answer is recorded when it arrives.

*MarketPay (scripted):* held open → abort 409 (too late: the customer just tapped) → no answer by 19 s

**Request**

```http
POST /payments
Content-Type: application/json

{
  "terminalId": "PAX:<TERMINAL_ID>",
  "amount": 1299,
  "currency": "SEK",
  "reference": "order-1005",
  "deadlineSeconds": 20
}
```

**Response** `201`

```json
{
  "amount": 1299,
  "createdAt": "2026-09-27T12:00:00Z",
  "currency": "SEK",
  "declineReason": null,
  "id": "9cf98eab-795b-56e5-a139-aa8c9cfeb523",
  "providerTransactionId": null,
  "reference": "order-1005",
  "reversed": false,
  "state": "unknown",
  "terminalId": "PAX:<TERMINAL_ID>",
  "updatedAt": "2026-09-27T12:00:19Z"
}
```

### Declined: a PARTIAL approval, reversed at once

A partial approval is never kept, never refunded (the approved amount is unknown); reversed by terminalTransactionId.

*MarketPay (scripted):* 201 PARTIAL → last-transaction (baseline) → cancel-transaction 200 OK

**Request**

```http
POST /payments
Content-Type: application/json

{
  "terminalId": "PAX:<TERMINAL_ID>",
  "amount": 1299,
  "currency": "SEK",
  "reference": "order-1006"
}
```

**Response** `201`

```json
{
  "amount": 1299,
  "createdAt": "2026-09-27T12:00:00Z",
  "currency": "SEK",
  "declineReason": "010",
  "id": "acfb88f5-0e30-50f8-8c0c-269f1641a382",
  "providerTransactionId": "14",
  "reference": "order-1006",
  "reversed": true,
  "state": "declined",
  "terminalId": "PAX:<TERMINAL_ID>",
  "updatedAt": "2026-09-27T12:00:00Z"
}
```

### Failed: refused by MarketPay (terminal offline, wrong currency)

*MarketPay (scripted):* process-transaction → 404 (empty body) → one confirming last-transaction look

**Request**

```http
POST /payments
Content-Type: application/json

{
  "terminalId": "PAX:<TERMINAL_ID>",
  "amount": 1299,
  "currency": "SEK",
  "reference": "order-1007"
}
```

**Response** `201`

```json
{
  "amount": 1299,
  "createdAt": "2026-09-27T12:00:00Z",
  "currency": "SEK",
  "declineReason": null,
  "id": "ca97d8d2-6949-5f2b-aa65-0994670c5442",
  "providerTransactionId": null,
  "reference": "order-1007",
  "reversed": false,
  "state": "failed",
  "terminalId": "PAX:<TERMINAL_ID>",
  "updatedAt": "2026-09-27T12:00:01Z"
}
```

### The same order again: returns the existing payment

`200` instead of `201`. MarketPay is not called again.

**Request**

```http
POST /payments
Content-Type: application/json

{
  "terminalId": "PAX:<TERMINAL_ID>",
  "amount": 1299,
  "currency": "SEK",
  "reference": "order-1008"
}
```

**Response** `200`

```json
{
  "amount": 1299,
  "createdAt": "2026-09-27T12:00:00Z",
  "currency": "SEK",
  "declineReason": null,
  "id": "373f2349-f676-5770-a54d-6ac35d35b3b6",
  "providerTransactionId": "14",
  "reference": "order-1008",
  "reversed": false,
  "state": "approved",
  "terminalId": "PAX:<TERMINAL_ID>",
  "updatedAt": "2026-09-27T12:00:00Z"
}
```

### The same reference, a different order

**Request**

```http
POST /payments
Content-Type: application/json

{
  "terminalId": "PAX:<TERMINAL_ID>",
  "amount": 500,
  "currency": "SEK",
  "reference": "order-1008"
}
```

**Response** `409`

```json
{
  "code": "idempotency_mismatch",
  "message": "Reference 'order-1008' is already used by payment 373f2349-f676-5770-a54d-6ac35d35b3b6 with a different amount 1299 (now 500). A new payment needs a new reference."
}
```

### The terminal is busy

order-1009 is still unresolved and holds the terminal; order-1010 is refused before anything is sent.

**Request**

```http
POST /payments
Content-Type: application/json

{
  "terminalId": "PAX:<TERMINAL_ID>",
  "amount": 1299,
  "currency": "SEK",
  "reference": "order-1010"
}
```

**Response** `409`

```json
{
  "code": "terminal_busy",
  "message": "Terminal PAX:<TERMINAL_ID> is locked by payment 2e445480-8e33-5498-b9b9-34898c2a49f2 (reference 'order-1009') until its outcome is confirmed."
}
```

### Validation error

**Request**

```http
POST /payments
Content-Type: application/json

{
  "terminalId": "pax:<terminal_id>",
  "amount": 0,
  "currency": "USD",
  "reference": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

**Response** `400`

```json
{
  "code": "validation_error",
  "message": "terminalId: String should match pattern '^[A-Z0-9]+:[^/\\s]+$'; amount: Input should be greater than or equal to 1; currency: Value error, unsupported currency; expected one of ['DKK', 'EUR', 'NOK', 'SEK']; reference: String should have at most 36 characters"
}
```

### Firestore unavailable before anything was sent

Retried for 5 s first. Nothing reached MarketPay, so repeating is safe.

**Request**

```http
POST /payments
Content-Type: application/json

{
  "terminalId": "PAX:<TERMINAL_ID>",
  "amount": 1299,
  "currency": "SEK",
  "reference": "order-1011"
}
```

**Response** `503`

```json
{
  "code": "store_unavailable",
  "message": "The payment datastore is unavailable. Nothing new was sent to the terminal; retry the same request (same reference) \u2014 it is safe to repeat."
}
```

---

## GET /payments/{id} and GET /payments — reads

Stored state only; never calls MarketPay.

### One payment

**Request**

```http
GET /payments/517ff2c4-3b9c-5cc7-925c-7a9e488d8f08
```

**Response** `200`

```json
{
  "amount": 1299,
  "createdAt": "2026-09-27T12:00:00Z",
  "currency": "SEK",
  "declineReason": null,
  "id": "517ff2c4-3b9c-5cc7-925c-7a9e488d8f08",
  "providerTransactionId": "14",
  "reference": "order-2001",
  "reversed": false,
  "state": "approved",
  "terminalId": "PAX:<TERMINAL_ID>",
  "updatedAt": "2026-09-27T12:00:00Z"
}
```

### One payment's history

Beyond the contract: one line per change of state, reason or terminal hold.

**Request**

```http
GET /payments/517ff2c4-3b9c-5cc7-925c-7a9e488d8f08/history
```

**Response** `200`

```json
{
  "items": [
    {
      "at": "2026-09-27T12:00:00Z",
      "basedOn": null,
      "detail": null,
      "number": 1,
      "operation": "purchase",
      "reason": null,
      "state": "pending",
      "summary": "pending: payment started; terminal held for the purchase"
    },
    {
      "at": "2026-09-27T12:00:00Z",
      "basedOn": "process_response",
      "detail": null,
      "number": 2,
      "operation": null,
      "reason": "bank_approved",
      "state": "approved",
      "summary": "approved (bank approved), from the payment's response; terminal released"
    }
  ],
  "paymentId": "517ff2c4-3b9c-5cc7-925c-7a9e488d8f08"
}
```

### Unknown id

**Request**

```http
GET /payments/00000000-0000-0000-0000-000000000000
```

**Response** `404`

```json
{
  "code": "not_found",
  "message": "No payment with id 00000000-0000-0000-0000-000000000000."
}
```

### List, first page

Newest first.

**Request**

```http
GET /payments?limit=2
```

**Response** `200`

```json
{
  "items": [
    {
      "amount": 1299,
      "createdAt": "2026-09-27T12:02:00Z",
      "currency": "SEK",
      "declineReason": null,
      "id": "f62ab57e-280d-5796-9ed4-2c17ccd4ca76",
      "providerTransactionId": "14",
      "reference": "order-2003",
      "reversed": false,
      "state": "approved",
      "terminalId": "PAX:<TERMINAL_ID>",
      "updatedAt": "2026-09-27T12:02:00Z"
    },
    {
      "amount": 1299,
      "createdAt": "2026-09-27T12:01:00Z",
      "currency": "SEK",
      "declineReason": "05",
      "id": "d9c11633-a547-50bd-b23e-5cda9c95be62",
      "providerTransactionId": "14",
      "reference": "order-2002",
      "reversed": false,
      "state": "declined",
      "terminalId": "PAX:<TERMINAL_ID>",
      "updatedAt": "2026-09-27T12:01:00Z"
    }
  ],
  "nextCursor": "eyJjcmVhdGVkX2F0IjoiMjAyNi0wOS0yN1QxMjowMTowMFoiLCJpZCI6ImQ5YzExNjMzLWE1NDctNTBiZC1iMjNlLTVjZGE5Yzk1YmU2MiJ9"
}
```

### List, next page (pass nextCursor back)

**Request**

```http
GET /payments?limit=2&cursor=eyJjcmVhdGVkX2F0IjoiMjAyNi0wOS0yN1QxMjowMTowMFoiLCJpZCI6ImQ5YzExNjMzLWE1NDctNTBiZC1iMjNlLTVjZGE5Yzk1YmU2MiJ9
```

**Response** `200`

```json
{
  "items": [
    {
      "amount": 1299,
      "createdAt": "2026-09-27T12:00:00Z",
      "currency": "SEK",
      "declineReason": null,
      "id": "517ff2c4-3b9c-5cc7-925c-7a9e488d8f08",
      "providerTransactionId": "14",
      "reference": "order-2001",
      "reversed": false,
      "state": "approved",
      "terminalId": "PAX:<TERMINAL_ID>",
      "updatedAt": "2026-09-27T12:00:00Z"
    }
  ],
  "nextCursor": null
}
```

### List with filters

**Request**

```http
GET /payments?state=approved,declined&terminalId=PAX:<TERMINAL_ID>&createdAfter=2026-09-27T12:00:30Z
```

**Response** `200`

```json
{
  "items": [
    {
      "amount": 1299,
      "createdAt": "2026-09-27T12:02:00Z",
      "currency": "SEK",
      "declineReason": null,
      "id": "f62ab57e-280d-5796-9ed4-2c17ccd4ca76",
      "providerTransactionId": "14",
      "reference": "order-2003",
      "reversed": false,
      "state": "approved",
      "terminalId": "PAX:<TERMINAL_ID>",
      "updatedAt": "2026-09-27T12:02:00Z"
    },
    {
      "amount": 1299,
      "createdAt": "2026-09-27T12:01:00Z",
      "currency": "SEK",
      "declineReason": "05",
      "id": "d9c11633-a547-50bd-b23e-5cda9c95be62",
      "providerTransactionId": "14",
      "reference": "order-2002",
      "reversed": false,
      "state": "declined",
      "terminalId": "PAX:<TERMINAL_ID>",
      "updatedAt": "2026-09-27T12:01:00Z"
    }
  ],
  "nextCursor": null
}
```

### A cursor the service didn't issue

**Request**

```http
GET /payments?cursor=garbage
```

**Response** `400`

```json
{
  "code": "validation_error",
  "message": "cursor: cursor is not one this service issued"
}
```

---

## POST /payments/{id}/cancel — cancel / reverse

See flows.md §2.

### Reverse an approved payment

The terminal asks the customer to tap again (a reversal is card-present on staging).

*MarketPay (scripted):* last-transaction (baseline) → cancel-transaction 200, status OK

**Request**

```http
POST /payments/f9e48a5b-b319-5abb-a9be-d07cdd23d8f9/cancel
```

**Response** `200`

```json
{
  "amount": 1299,
  "createdAt": "2026-09-27T12:00:00Z",
  "currency": "SEK",
  "declineReason": null,
  "id": "f9e48a5b-b319-5abb-a9be-d07cdd23d8f9",
  "providerTransactionId": "14",
  "reference": "order-3001",
  "reversed": true,
  "state": "cancelled",
  "terminalId": "PAX:<TERMINAL_ID>",
  "updatedAt": "2026-09-27T12:00:00Z"
}
```

### Cancel again: already cancelled

**Request**

```http
POST /payments/f9e48a5b-b319-5abb-a9be-d07cdd23d8f9/cancel
```

**Response** `200`

```json
{
  "amount": 1299,
  "createdAt": "2026-09-27T12:00:00Z",
  "currency": "SEK",
  "declineReason": null,
  "id": "f9e48a5b-b319-5abb-a9be-d07cdd23d8f9",
  "providerTransactionId": "14",
  "reference": "order-3001",
  "reversed": true,
  "state": "cancelled",
  "terminalId": "PAX:<TERMINAL_ID>",
  "updatedAt": "2026-09-27T12:00:00Z"
}
```

### Cancel a payment still waiting on the terminal

*MarketPay (scripted):* abort-transaction 204 → last-transaction shows it stopped (NOK)

**Request**

```http
POST /payments/ca646651-6cfb-5c20-ab74-b159d60c1147/cancel
```

**Response** `200`

```json
{
  "amount": 1299,
  "createdAt": "2026-09-27T12:00:00Z",
  "currency": "SEK",
  "declineReason": null,
  "id": "ca646651-6cfb-5c20-ab74-b159d60c1147",
  "providerTransactionId": "14",
  "reference": "order-3002",
  "reversed": false,
  "state": "cancelled",
  "terminalId": "PAX:<TERMINAL_ID>",
  "updatedAt": "2026-09-27T12:00:59Z"
}
```

### Cancel arrives after the terminal already gave up

The cancel first takes one look; it shows the payment ended NOK (no card in time), so it is settled as `failed` — nothing was charged, nothing to cancel.

*MarketPay (scripted):* last-transaction shows it NOK

**Request**

```http
POST /payments/10e569ce-11db-59bd-aea1-5c47bb5d9a19/cancel
```

**Response** `409`

```json
{
  "code": "not_cancellable",
  "message": "Payment 10e569ce-11db-59bd-aea1-5c47bb5d9a19 is failed: nothing was charged, so there is nothing to cancel."
}
```

### The reversal didn't take effect (e.g. nobody tapped): still approved

*MarketPay (scripted):* cancel-transaction 200, status NOK

**Request**

```http
POST /payments/3fec1b5b-34cb-5084-8ba4-dacb5a278be0/cancel
```

**Response** `409`

```json
{
  "code": "cancel_failed",
  "message": "Payment 3fec1b5b-34cb-5084-8ba4-dacb5a278be0 is still approved: the reversal did not take effect (undo_refused)."
}
```

### Nothing to cancel (declined)

**Request**

```http
POST /payments/d810e73d-6943-56e3-a340-38b5a8064aba/cancel
```

**Response** `409`

```json
{
  "code": "not_cancellable",
  "message": "Payment d810e73d-6943-56e3-a340-38b5a8064aba is declined: nothing was charged, so there is nothing to cancel."
}
```

### A partial reversal: needs a person

*MarketPay (scripted):* cancel-transaction 200, status PARTIAL

**Request**

```http
POST /payments/cae26fbb-46bd-50e8-b2d7-1ebbac26e2ea/cancel
```

**Response** `409`

```json
{
  "code": "needs_attention",
  "message": "Payment cae26fbb-46bd-50e8-b2d7-1ebbac26e2ea cannot be cancelled automatically (partial_reversal); it needs manual review."
}
```

### Unknown id

**Request**

```http
POST /payments/00000000-0000-0000-0000-000000000000/cancel
```

**Response** `404`

```json
{
  "code": "not_found",
  "message": "No payment with id 00000000-0000-0000-0000-000000000000."
}
```

---

## POST /reconcile — recover open payments

See flows.md §3.

### After a crash: the payment is settled from last-transaction

The process died after MarketPay approved but before it was recorded; the restarted process (new boot id) takes it over.

*MarketPay (scripted):* last-transaction shows order-4001 approved

**Request**

```http
POST /reconcile
```

**Response** `200`

```json
{
  "resolved": 1,
  "resolvedIds": [
    "90d324a6-8f23-5d10-bcc2-ac7661e28091"
  ],
  "scanned": 1,
  "stillOpen": 0
}
```

### Again: nothing left to do (idempotent)

**Request**

```http
POST /reconcile
```

**Response** `200`

```json
{
  "resolved": 0,
  "resolvedIds": [],
  "scanned": 0,
  "stillOpen": 0
}
```

### Limited to one terminal

**Request**

```http
POST /reconcile
Content-Type: application/json

{
  "terminalId": "PAX:<TERMINAL_ID>"
}
```

**Response** `200`

```json
{
  "resolved": 0,
  "resolvedIds": [],
  "scanned": 0,
  "stillOpen": 0
}
```

### Invalid body

**Request**

```http
POST /reconcile
Content-Type: application/json

{
  "olderThan": "yesterday"
}
```

**Response** `400`

```json
{
  "code": "validation_error",
  "message": "olderThan: Input should be a valid datetime or date, input is too short"
}
```

---

## Diagnostics (beyond the contract)

See api-extensions.md.

### Liveness

**Request**

```http
GET /healthz
```

**Response** `200`

```json
{
  "status": "ok"
}
```

### Readiness (Firestore reachable)

**Request**

```http
GET /readyz
```

**Response** `200`

```json
{
  "firestore": "ok",
  "status": "ok"
}
```

### Terminals, with the service's lock state

**Request**

```http
GET /terminals
```

**Response** `200`

```json
{
  "items": [
    {
      "connected": true,
      "locked": false,
      "lockedBy": null,
      "terminalId": "PAX:<TERMINAL_ID>",
      "wsCreatedTime": "2026-09-28T01:29:30.047915Z"
    }
  ]
}
```

### The terminal's last transaction, as parsed

**Request**

```http
GET /terminals/PAX:<TERMINAL_ID>/last-transaction
```

**Response** `200`

```json
{
  "lastTransactionState": "FINISHED",
  "transactionResult": {
    "finalTransactionParams": {
      "amount": "1299",
      "ecrTransactionId": "order-1001"
    },
    "responseCode": "000",
    "status": "OK",
    "terminalTransactionId": "<TERMINAL_TRANSACTION_ID>"
  }
}
```

### Readiness (Firestore unreachable)

**Request**

```http
GET /readyz
```

**Response** `503`

```json
{
  "firestore": "RuntimeError: no route to Firestore",
  "status": "unavailable"
}
```

### MarketPay unreachable

**Request**

```http
GET /terminals
```

**Response** `502`

```json
{
  "code": "provider_unavailable",
  "message": "No answer from MarketPay for GET /terminals: ConnectError. A connection reset usually means the mTLS certificate was rejected."
}
```
