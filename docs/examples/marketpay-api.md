# MarketPay Cloud API: request and response examples

What the service sends to MarketPay (pre-production, `https://marketpay.example.test`, API v1.1.14)
and what it **actually** answers. Every example is labelled with where it comes from:

| Label | Meaning |
|---|---|
| **Observed** | A real response from staging. |
| **Observed (parsed)** | Seen through the service, which drops fields it doesn't model; the raw body had more. |
| **Spec only** | Never seen on staging: the shape from [`api.json`](../market-pay-api.json), shown so every case has an example. |

Card data from the staging test card is **redacted**: the BIN (`bankId`) and the last four
digits are shown as `••••••` / `••••`.

Examples of the service's own API: [service-api.md](service-api.md).

- [Every request](#every-request)
- [GET /terminals](#get-terminals)
- [POST /process-transaction](#post-process-transactionterminal-id)
- [GET /last-transaction](#get-last-transactionterminal-id)
- [POST /cancel-transaction](#post-cancel-transactionterminal-id)
- [POST /abort-transaction](#post-abort-transactionterminal-id)
- [POST /update-terminal (not used)](#post-update-terminalterminal-id-not-used)
- [Notifications (the bonus webhook)](#notifications-the-bonus-webhook)

---

## Every request

- **Mutual TLS:** the client certificate and key (`CLIENT_CERT.crt` / `CLIENT_CERT.key`) on every
  connection. A missing, expired or unprovisioned certificate shows up as a **connection reset**,
  with no HTTP status at all (integration guide §2.8).
- **`User-Agent`:** any non-empty value (the service sends `TestAssignment/1.0`).
- **Terminal id** in the path: `PAX:<TERMINAL_ID>` (uppercase manufacturer).

| Request problem | Answer | Label |
|---|---|---|
| No `User-Agent` | `400`, **empty body** | Observed |
| Lowercase manufacturer (`pax:<terminal_id>`) | `404`, **empty body** | Observed (`last-transaction`) |
| Unknown / not-connected terminal | `404`, **empty body** | Observed (every operation) |
| DNS failure (after the laptop woke) | no answer: `ConnectError: Name or service not known` | Observed |

---

## `GET /terminals`

```http
GET /terminals?storeCode=<store-code>
User-Agent: TestAssignment/1.0
```

**`200`: the terminals of the store.** Observed. This is a **bare array**; the service's
own `GET /terminals` wraps it and adds the lock state (see [api-extensions.md](../api-extensions.md)).

```json
[{"terminalId":"PAX:<TERMINAL_ID>","connected":true,"wsCreatedTime":"2026-09-28T01:29:30.047915745Z"}]
```

**`200` with a filter that matches nothing** (`&connected=false`): Observed.

```json
[]
```

**`400` without `storeCode`:** Observed. A JSON error body, unlike the other `400`s.

```json
{"timestamp":"2026-09-28T07:03:12.288Z","status":400,"error":"Bad Request","path":"/terminals"}
```

---

## `POST /process-transaction/{terminal-id}`

**The request the service sends** (exactly, from `ProcessTransactionRequest`). `waitTime` is the
deadline + 90 s (150 for the default 60), so MarketPay holds the request open past the service's
deadline.

```http
POST /process-transaction/PAX:<TERMINAL_ID>?waitTime=150
User-Agent: TestAssignment/1.0
Content-Type: application/json

{
  "ecrTransactionId": "order-example",
  "amount": "1299",
  "currency": "752",
  "transactionType": "PURCHASE",
  "ecrParams": {"ecrId": "<ECR_ID>"}
}
```

For a refund the service sends the same shape with `"transactionType": "REFUND"` and the
refund's own `ecrTransactionId` (`rf01<payment id hex>`). The amount and currency are **strings**.

### `201`: approved

**Observed (parsed): contactless approval.** This is the service's view, which drops the fields it
doesn't model. The full set of fields of an approved record is in the raw `last-transaction`
example below.

```json
{
  "authorizationCode": "213462",
  "cardData": {"cardCapture": "CONTACTLESS", "extractedPan": "XXXXXXXXXXXX••••"},
  "finalTransactionParams": {"amount": "100", "currency": "752", "ecrTransactionId": "order-approved"},
  "responseCode": "000",
  "status": "OK",
  "terminalTransactionId": "<TERMINAL_TRANSACTION_ID>"
}
```

- `authorizationCode` is **always `213462`** on staging, the spec's own example value.
- **`transactionType` is never echoed**, though the spec calls it required.

### `201`: stopped on the terminal (Cancel pressed / no card in time / the service's abort)

**Observed:** terminal cancel, no card in time, and the service's abort produce the same shape. The
`201` body carries the same `TransactionResult` that `last-transaction` shows. Raw:

```json
{
  "cardData": {"loyaltyErrorFlag": false},
  "dccDetails": {"dccAmount": 0, "dccCurrency": 0, "dccOffered": false, "dccUsed": false},
  "finalTransactionParams": {
    "ecrTransactionId": "order-stopped",
    "amount": "0",
    "currency": "0",
    "amountCashback": "0",
    "amountTip": "0"
  },
  "forcedOffline": false,
  "signatureRequired": false,
  "status": "NOK",
  "terminalTransactionId": "<TERMINAL_TRANSACTION_ID>"
}
```

- **No `responseCode`**, although the spec calls it required. That's how the service tells
  "stopped before the bank" (`failed`) from a bank decline (`declined`, which has a code).
- `amount` and `currency` are echoed as **`"0"`**. Only `ecrTransactionId` is trustworthy in a
  NOK echo.

### `201`: declined by the bank

**Spec only.** Staging's simulated host approved every card presented. A decline carries
`status: NOK` **and** an acquirer `responseCode` (any value other than `000`); `authorizationCode`
is absent.

```json
{
  "status": "NOK",
  "responseCode": "116",
  "terminalTransactionId": "<TERMINAL_TRANSACTION_ID>",
  "finalTransactionParams": {"ecrTransactionId": "order-declined", "amount": "1299", "currency": "752"}
}
```

### `201`: partial approval

**Spec only** (`status: PARTIAL`). Never produced by staging. The service reverses it and reports
`declined`.

### `202`: accepted, not finished within `waitTime`

**Observed:** no body. It's not an outcome: the payment may still complete, and after a `202` an
abort can no longer stop it.

### `400`: a missing required field

**Observed** without `transactionType`, sent to an unknown terminal:

```json
{"timestamp":"2026-09-28T07:03:39.232Z","status":400,"error":"Bad Request","path":"/process-transaction/PAX:<UNKNOWN_TERMINAL_ID>"}
```

The same request with the amount as a **number** (`"amount": 100`) got **`404`**, not `400`. The
terminal seems to be checked before the body is validated.

### `404`: terminal not connected / lowercase manufacturer / currency mismatch

**Observed:** empty body. It proves nothing started, but only after one confirming look.

### `500`

**Spec only** for this endpoint (seen on `last-transaction`, below). The service treats it like a
lost reply.

---

## `GET /last-transaction/{terminal-id}`

```http
GET /last-transaction/PAX:<TERMINAL_ID>
User-Agent: TestAssignment/1.0
```

Only one call per terminal at a time: every **overlapping** call answers `500`.

### `200`: a stopped payment or refund (FINISHED, NOK)

**Observed, raw**: an aborted reversal because nobody tapped. It has the same shape as a stopped
purchase.

```json
{
  "lastTransactionState": "FINISHED",
  "transactionResult": {
    "cardData": {"loyaltyErrorFlag": false},
    "dccDetails": {"dccAmount": 0, "dccCurrency": 0, "dccOffered": false, "dccUsed": false},
    "finalTransactionParams": {
      "ecrTransactionId": "order-reversal",
      "amount": "0",
      "currency": "0",
      "amountCashback": "0",
      "amountTip": "0"
    },
    "forcedOffline": false,
    "signatureRequired": false,
    "status": "NOK",
    "terminalTransactionId": "<TERMINAL_TRANSACTION_ID>"
  }
}
```

### `200`: a reversal (FINISHED, OK), the full set of fields

**Observed, raw**: an approved reversal. This is how staging reports a reversal: **no
`cancellationResult`**, a normal `transactionResult` with the **purchase's** `ecrTransactionId`
and a **new** `terminalTransactionId`, and no `transactionType`. Only the receipt says `REFUND`.
An approved purchase record has the same fields.

```json
{
  "lastTransactionState": "FINISHED",
  "transactionResult": {
    "responseCode": "000",
    "authorizationCode": "213462",
    "cardData": {
      "applicationId": "A0000000031010",
      "bankId": "••••••",
      "cardCapture": "CONTACTLESS",
      "extractedPan": "XXXXXXXXXXXX••••",
      "loyaltyErrorFlag": false,
      "panSequenceNumber": "01",
      "schema": "VISA"
    },
    "cashierReceipt": "…(350 characters, below)…",
    "customerReceipt": "…(275 characters, below)…",
    "dccDetails": {"dccAmount": 0, "dccCurrency": 0, "dccOffered": false, "dccUsed": false},
    "finalTransactionParams": {
      "ecrTransactionId": "order-reversal",
      "amount": "100",
      "currency": "752",
      "amountCashback": "0",
      "amountTip": "0",
      "cashierId": "cloud"
    },
    "forcedOffline": false,
    "merchantId": "<MERCHANT_ID>",
    "signatureRequired": false,
    "status": "OK",
    "terminalId": "<DEVICE_SERIAL>",
    "terminalTransactionId": "<REVERSAL_TERMINAL_TRANSACTION_ID>"
  }
}
```

`terminalId` here is the **device serial**, not the `PAX:…` connection id.
`cashierReceipt`, decoded (`\n` as line breaks):

```text
A0000000031010
VISA CONTACTLESS
27-09-2026 17:03
<MERCHANT_NAME>
<MERCHANT_LOCATION>
<MERCHANT_ADDRESS>
<MERCHANT_ID>
XXXXXXXXXXXX•••• 01
EFE5F75F
REFERENCE NO.: <REVERSAL_TERMINAL_TRANSACTION_ID>
REFERENCE: order-reversal
AUTHORIZATION
CODE: 213462
AMOUNT:SEK 1,00
REFUND
Approved
MERCHANT RECEIPT
PLEASE SIGN BELOW



---------------------------------
SIGNATURE REQUIRED
PLEASE RETAIN FOR YOUR RECORDS
```

`customerReceipt` is the same down to `Approved`, then ends with `CARDHOLDER RECEIPT` and
`PLEASE RETAIN FOR YOUR RECORDS`.

### `200`: an approved purchase

**Observed (parsed):** see `201: approved` above. **Observed (raw fields):** as for the full raw
record above.

### `200`: while the service's payment is still running

**Observed:** **the previous finished transaction**, never this payment's, and never `IN_PROGRESS`.
So a lookup during a payment returns another payment's record, like the two above, with a different
`ecrTransactionId`.

### `200`: `IN_PROGRESS`, `NOT_FOUND`, `cancellationResult`

**Spec only**, never observed. The service still handles them.

```json
{"lastTransactionState": "IN_PROGRESS"}
```

```json
{"lastTransactionState": "NOT_FOUND"}
```

```json
{
  "lastTransactionState": "FINISHED",
  "cancellationResult": {
    "status": "OK",
    "cancellationParams": {
      "terminalTransactionId": "<TERMINAL_TRANSACTION_ID>",
      "ecrTransactionId": "order-cancelled",
      "amount": "1299",
      "currency": "752"
    }
  }
}
```

### `500`: transient

**Observed, many times**: **empty body**. Sometimes right after a `202`, sometimes when two calls
overlap, and sometimes twice in a row with nothing else calling. A retry a second or two later
answers `200`.

### `404`: unknown / not-connected terminal, or lowercase manufacturer

**Observed**: **empty body**.

---

## `POST /cancel-transaction/{terminal-id}`

**The request the service sends** (exactly, from `CancelTransactionRequest`). Every field describes
the **original** payment.

```http
POST /cancel-transaction/PAX:<TERMINAL_ID>?waitTime=149
User-Agent: TestAssignment/1.0
Content-Type: application/json

{
  "terminalTransactionId": "<ORIGINAL_TERMINAL_TRANSACTION_ID>",
  "ecrTransactionId": "order-reversal",
  "amount": "100",
  "currency": "752",
  "ecrParams": {"ecrId": "<ECR_ID>"}
}
```

On staging the terminal then **asks the customer to tap the card again** and runs it as a
`REFUND`. It took roughly 10–20 s with a tap.

### `200`: the cancellation's result

**Observed (parsed):** `status: OK`, and `status: NOK` after the service's abort when nobody tapped.
The raw body wasn't captured, so here is the spec's shape:

```json
{
  "status": "OK",
  "cancellationParams": {
    "terminalTransactionId": "<ORIGINAL_TERMINAL_TRANSACTION_ID>",
    "ecrTransactionId": "order-reversal",
    "amount": "100",
    "currency": "752"
  },
  "cashierReceipt": "REFUND\nAMOUNT:SEK 1,00\nTRANSACTION RESULT: Approved\nMERCHANT RECEIPT\n",
  "customerReceipt": "…"
}
```

`status` can also be `PARTIAL` (spec only) → the service flags it for a person.

### `202`

**Spec only.** No body; not an outcome. The service then watches `last-transaction` for the
reversal's own record, and never re-sends.

### `404`: unknown / not-connected terminal

**Observed**: **empty body**.

---

## `POST /abort-transaction/{terminal-id}`

**The request the service sends.** The body is just `EcrParams`.

```http
POST /abort-transaction/PAX:<TERMINAL_ID>
User-Agent: TestAssignment/1.0
Content-Type: application/json

{"ecrId": "<ECR_ID>"}
```

| Answer | When | Label |
|---|---|---|
| **`204`**, no body | while **the service's** payment/reversal request is still open (the terminal stops, and the open request returns the NOK about a second later) | Observed |
| **`409`** (body not captured) | after a `202`, after the service's process crashed, or on an idle terminal. The spec says "too late, may have completed"; on staging it carries **no information**. | Observed |
| `404`, empty body | unknown / not-connected terminal | Observed |
| `500` | — | Spec only |

---

## `POST /update-terminal/{terminal-id}` (not used)

**Spec only.** It asks the terminal to update its software (2–3 minutes). The service never calls
it; it's listed for completeness.

```http
POST /update-terminal/PAX:<TERMINAL_ID>
User-Agent: TestAssignment/1.0
Content-Type: application/json

{"duration": 120, "ecrParams": {"ecrId": "<ECR_ID>"}}
```

```json
{"success": true}
```

The `404` and `500` answers have the same shape, with `"success": false` and an `errorMessage`.

---

## Notifications (the bonus webhook)

With `ecrParams.notificationUrl` set, MarketPay POSTs progress and the final result to that URL,
once each, **with no retry** (not in `api.json`; the integration guide §3.3). How the service uses
them: [flows §9](../flows.md#9-marketpay-notifications-webhook).

### Final notification: approved

**Observed** via a temporary online webhook. Card data redacted; receipts shortened.

```json
{
  "ecrId": "<ECR_ID>",
  "ecrTransactionId": "order-notification-approved",
  "status": "COMPLETED",
  "terminalTransactionId": null,
  "result": {
    "status": "OK",
    "responseCode": "000",
    "authorizationCode": "213462",
    "terminalTransactionId": "<TERMINAL_TRANSACTION_ID>",
    "terminalId": "<DEVICE_SERIAL>",
    "merchantId": "<MERCHANT_ID>",
    "signatureRequired": false,
    "forcedOffline": false,
    "issuerOption": null,
    "parBank": null,
    "privateData": null,
    "cardData": {
      "applicationId": "A0000000031010",
      "bankId": "XXXXXX",
      "cardCapture": "CONTACTLESS",
      "extractedPan": "XXXXXXXXXXXXXXXX",
      "loyaltyErrorFlag": false,
      "loyaltyId": null,
      "panSequenceNumber": "01",
      "schema": "VISA"
    },
    "dccDetails": {"dccAmount": 0, "dccCurrency": 0, "dccOffered": false, "dccUsed": false},
    "finalTransactionParams": {
      "ecrTransactionId": "order-notification-approved",
      "amount": 100,
      "currency": 752,
      "amountCashback": 0,
      "amountTip": 0,
      "cashierId": "cloud",
      "merchantOption": null,
      "mode": "DIRECT",
      "transactionReference": null,
      "type": "PURCHASE"
    },
    "cashierReceipt": "A0000000031010\nVISA CONTACTLESS\n… REFERENCE NO.: <TERMINAL_TRANSACTION_ID>\nREFERENCE: order-notification-approved\nAUTHORIZATION\nCODE: 213462\nAMOUNT:SEK 1,00\nPAYMENT\nApproved\nMERCHANT RECEIPT\n…",
    "customerReceipt": "… the same, CARDHOLDER RECEIPT …"
  }
}
```

A payment answered `201` gets the **same** notification.

Compared with the guide's example below: the envelope's `terminalTransactionId` is `null` (the
result has it); the result carries `finalTransactionParams`, whose `amount` and `currency` are
**numbers** (strings in `last-transaction`); the type is echoed as **`type`** (never as
`transactionType`).

### Final notification: no card presented

**Observed**. The terminal's own timeout: NOK, no `responseCode`, card fields `null`, the real
amount echoed (unlike `last-transaction`'s NOK).

```json
{
  "ecrId": "<ECR_ID>",
  "ecrTransactionId": "order-notification-timeout",
  "status": "COMPLETED",
  "terminalTransactionId": null,
  "result": {
    "status": "NOK",
    "responseCode": null,
    "authorizationCode": null,
    "terminalTransactionId": "<TERMINAL_TRANSACTION_ID>",
    "terminalId": null,
    "merchantId": null,
    "signatureRequired": false,
    "forcedOffline": false,
    "issuerOption": null,
    "parBank": null,
    "privateData": null,
    "cardData": {
      "applicationId": null,
      "bankId": null,
      "cardCapture": null,
      "extractedPan": null,
      "loyaltyErrorFlag": false,
      "loyaltyId": null,
      "panSequenceNumber": null,
      "schema": null
    },
    "dccDetails": {"dccAmount": 0, "dccCurrency": 0, "dccOffered": false, "dccUsed": false},
    "finalTransactionParams": {
      "ecrTransactionId": "order-notification-timeout",
      "amount": 100,
      "currency": 752,
      "amountCashback": 0,
      "amountTip": 0,
      "cashierId": null,
      "merchantOption": null,
      "mode": "DIRECT",
      "transactionReference": null,
      "type": "PURCHASE"
    },
    "cashierReceipt": null,
    "customerReceipt": null
  }
}
```

### Progress, and the guide's final example

**From the integration guide §3.3.** Not observed: no progress notification was sent for either
transaction above.

```json
{"status": "WAITING_FOR_CARD", "ecrId": "POS_01", "ecrTransactionId": "ECR-mrt1levt-eqi21j"}
```

```json
{
  "status": "COMPLETED",
  "ecrId": "POS_01",
  "ecrTransactionId": "ECR-mrt1levt-eqi21j",
  "terminalTransactionId": "<TERMINAL_TRANSACTION_ID>",
  "result": {
    "status": "OK",
    "responseCode": "000",
    "authorizationCode": "213462",
    "terminalTransactionId": "<TERMINAL_TRANSACTION_ID>",
    "signatureRequired": false,
    "forcedOffline": false,
    "customerReceipt": "...",
    "cashierReceipt": "..."
  }
}
```

The other progress states are `PIN_REQUIRED` and `BANK_AUTHORIZATION`.
