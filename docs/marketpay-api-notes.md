# MarketPay Cloud API: observed behaviour and notes

A working log of how the **pre-production** MarketPay Cloud API (v1.1.14) and the terminal
`PAX:TEST_TERMINAL` actually behave, compared with the spec (`api.json` in the initial task)
and the integration guide. It feeds the README's *model risk* and *failure model* sections.

Each finding records **the observed behaviour**, **what the spec says**, and **the impact on the design**, with its status:

- ✅ **handled**: the code accounts for it (a test covers it).
- ❓ **open**: not verified; a question for MarketPay, or an assumption the service relies on.

---

## Status

None of the findings below is wrong in the code: each is handled, and covered by tests. The
questions they raise for MarketPay are in [open-questions.md](open-questions.md).

---

## Findings

### Connectivity and access

**mTLS and User-Agent work as documented.** ✅
`GET /terminals?storeCode=…` answers 200 with the client certificate and a `User-Agent`
header. The key and certificate are mounted read-only and never committed.

**an unknown terminal id gets 404 on `process-transaction`.** ✅
`process-transaction` and `last-transaction` both answer `404` for an unknown terminal id. The
spec gives three causes for this 404: the terminal is not connected, the manufacturer is not
uppercase, or the currency doesn't match. *Handled:* a `400` or `404` becomes `failed` /
`provider_rejected`, after one look at `last-transaction`. That look catches a
network-duplicated copy of the service's request that did run. Any **other** 4xx is undocumented
here, might mean "busy", and is handled like a lost answer.

**DNS can fail right after the machine wakes up.** ✅
The client can get `ConnectError: [Errno -2] Name or service not known` inside the container. In
that case the request never left the service. *Handled:* connect-phase errors are classified as
`NotSent` and **re-sent** with the same `ecrTransactionId`, with backoff, for up to 15 s. Only
after that → `failed` / `not_sent`, and the terminal is freed.

### Transaction results

**NOK results leave out fields the spec calls required.** ✅
Some NOK records have no `responseCode`; `amount` and `currency` are echoed as `"0"`, and
`cardData` holds only `loyaltyErrorFlag`. The spec marks `responseCode` as required.
*Handled:* `responseCode` is optional in the service's model, `declineReason` is `null` when
there's no code (the service doesn't invent one), and from the echo the service trusts only
`ecrTransactionId`.

**"Cancel pressed on the terminal" can't be told apart from "no card in time".** ✅ ❓
A terminal-side cancel and a terminal timeout produce identical records: NOK, no `responseCode`,
no card data. The only difference is timing.
*Handled:* NOK **with** an acquirer `responseCode` → `declined` (the bank said no). NOK
**without** one → `failed` / `terminal_stopped` (it stopped before the bank).
❓ Ask MarketPay whether a documented code for a terminal cancel exists.

**staging approvals look simulated.** ❓
Every approval carries `authorizationCode: "213462"` (the spec's example value) and
`merchantId: MERCHANT_ID`. Test card taps were used; no money appears to have moved, but nothing
visible from here proves it.

**`transactionType` is never echoed back.** ✅ ❓
The service always sends `"transactionType": "PURCHASE"` (or `"REFUND"`). The spec lists
`finalTransactionParams.transactionType` as **required**. Yet **no record** MarketPay has
returned includes it, whatever the kind of operation:

| Record type | Kind | `finalTransactionParams` as returned |
|---|---|---|
| Approved purchase | purchase, **approved** | `{ecrTransactionId, amount: "100", currency: "752"}` |
| Stopped purchase | purchase, NOK (no card / Cancel pressed) | `{ecrTransactionId, amount: "0", currency: "0", amountCashback: "0", amountTip: "0"}` |
| Approved reversal | **reversal**, OK | `{ecrTransactionId, amount, currency, amountCashback, amountTip, cashierId}` |
| Stopped reversal | **reversal**, NOK (aborted, nobody tapped) | `{ecrTransactionId, amount: "0", currency: "0", amountCashback: "0", amountTip: "0"}` |

*Impact:* the type, which should tell a purchase from a reversal or a refund, is unusable.
- **A purchase and its reversal** carry the same `ecrTransactionId`, so the service tells them
  apart by `terminalTransactionId` plus a baseline (see the reversal finding below). Only the receipt text says `REFUND`.
- **A refund** gets its own `ecrTransactionId` (`rf01…`), so it can't be confused with the
  purchase.
- *Handled:* the field is optional in the service's model and **no decision in the code reads it**
  (it only appears in the `status`/`responseCode` inconsistency log).
- ❓ Open question: does production echo it?

**the result's `terminalId` is the device serial, not the connection id.** ✅
Result `terminalId: "DEVICE_SERIAL"` versus connection id `PAX:TEST_TERMINAL`, as the spec warns.
The service never uses the result's `terminalId`.

### Timing

**`waitTime` is honoured exactly.** ✅
`process-transaction` holds for roughly the requested `waitTime` and then answers `202` when the
transaction has not finished.

**the terminal's own card-wait timeout is about 120 s.** ✅
The terminal can keep showing "present card" for about 2 minutes, even after an abort that is too
late to stop it (see *`abort-transaction`* below). The terminal then records NOK without a
`responseCode` (see *Transaction results* above).

### `last-transaction`

**a running transaction is never reported.** ✅ ❓
While the service's transaction ran, `last-transaction` kept returning the **previous, FINISHED**
transaction. `IN_PROGRESS` was never observed. The spec defines `IN_PROGRESS`.
*Impact:* "not this payment's record" is ambiguous. It may mean *this payment is still running*,
or *this payment never arrived / failed very late*. Only a FINISHED record of this payment is
evidence.
*Handled:* recovery waits for the record. A time rule treats "no record 180 s after sending"
as never ran → `failed` / `never_recorded` (`TERMINAL_MAX_TRANSACTION_SECONDS`, the terminal's
~120 s card timeout with a margin). ❓ This 180 s is the service's assumption; card, PIN and a slow issuer together
might in theory take longer.

**transient HTTP 500s, often right after a `202`.** ✅ (partly explained by the next finding)
Retries can see empty-body HTTP 500s, often right after a `202`. A retry a second later can answer
200. *Handled:* every lookup is retried and a failed lookup is never an outcome.

**overlapping `last-transaction` calls for one terminal all fail with HTTP 500.** ✅
When a manual call and the service's own poll overlapped by a few milliseconds, both got `500`.
Every poll before and after got `200`. Measured directly against MarketPay on an idle terminal:

| Pattern | Result |
|---|---|
| One at a time, 1 s apart | `200` ×5 |
| Back to back, no gap | `200` ×8 |
| Two at once | `500 500` in 5 of 5 rounds |
| Three at once | `500 500 200` in 3 of 3 rounds |

A single call takes about 1.1–1.3 s, which suggests MarketPay asks the terminal itself each
time and can't serve two at once. This may also explain some of the "random" 500s above — but not
all: a direct call can also answer `500` twice in a row, 2 s apart, with nothing else known to be
calling, then `200`.
*Handled:* the service's client allows **one `last-transaction` per terminal at a time**. Callers
queue, within their own timeout, instead of colliding; other terminals are unaffected. In a
concurrency check, three simultaneous calls through the service returned `200 200 200` ×3.
❓ This only covers **the service's own process**. Anyone else calling `last-transaction` for the
same terminal at the same moment (another service instance, someone in Postman, **a test suite
comparing the service's records with MarketPay's**) still makes both calls fail. The service
retries its own calls and a failed lookup is never an outcome, so this is harmless for
correctness, but the other caller may not retry. The README says so under Scope and limits.

**after a reversal there is no `cancellationResult`.** ✅ (and no `transactionType` either, see above)
After reversing a purchase, `last-transaction` returned a `transactionResult` with the purchase's
same `ecrTransactionId`, `status: OK`, `responseCode: 000`, a new `terminalTransactionId`, no
`transactionType`, and receipt text `REFUND … Approved`. There is no `cancellationResult`,
although the spec defines that field for exactly this case.
*Handled:* a FINISHED record with the payment's `ecrTransactionId` counts as *this* reversal only
if its `terminalTransactionId` is new, meaning neither the purchase's nor a baseline taken just
before the reversal was sent (while the service holds the terminal, so nothing else runs). The
baseline matters for a second attempt, when the terminal may still show the first attempt's record.
The spec's `cancellationResult` shape is still recognised too.

**`terminalTransactionId` counts every terminal operation.** ✅
It increases for every operation, including failures and reversals. *Impact:*
`ecrTransactionId` alone does **not** identify one operation, because a reversal reuses it. The
pair (`ecrTransactionId`, `terminalTransactionId`) does.

**`process-transaction` once answered an undocumented `409`.** ❓ (seen once)
The spec lists 201/202/400/404/500 only, but staging has also answered `409` without starting
anything visible on the terminal. *Handled already:* an undocumented 4xx proves nothing, so it is
watched, aborted (`204`) and confirmed before becoming `failed` / `aborted`, the correct state for
"never started". *Open:* a question for MarketPay.

### `abort-transaction`

**`204` only while the service's request is still open.** ✅ (confirmed live)

| When | Result |
|---|---|
| While the service's `process-transaction` request was still open | **204**; the open request returned `201` NOK about a second later |
| The service's deadline abort, `process-transaction` held open | **204**; the open request returned `201` NOK about a second later; screen cleared |
| The service's deadline abort, `cancel-transaction` held open, nobody tapped for the reversal | **204**; the open request returned `200`, cancellation NOK, about a second later; terminal free |
| After MarketPay answered `202` | **409**; the terminal kept showing "present card" for about 2 minutes |
| After the service's process crashed (request gone) | **409**; the terminal ended it on its own timeout |
| Idle terminal (nothing running) | **409** |

The spec says 409 means "too late — the payment may have completed". On staging, 409 means
**"nothing I can abort"** and carries no information about the payment either way.
*Handled already:* only a `204` counts as "stopped". A `409` never turns "no record" into
`failed`, and the `aborted` label is used only after a `204`.
*Handled:* `waitTime` now runs past the service's deadline (deadline + 90 s), and the call runs on
a worker thread. At the abort point the abort is sent **while the call is still open**, and the
open call returns the terminal's NOK within about a second. An answer that
comes only after the deadline is recorded when it arrives. Reversals and refunds work the same way.
❓ Could the service's crash-recovery ever stop a transaction? Apparently not, since its request
died with the process.

### `cancel-transaction` (reversal)

**on this terminal a reversal is a REFUND that needs the card.** ✅ ❓
After `cancel-transaction` the terminal asked the customer to **present the card again**. The call
returned `200` with a `CancellationResult` of `status: OK`; the receipt reads `REFUND`.
*Impact:* a reversal is customer-interactive and slow, like a payment. It can time out if the
customer has gone. *Handled:* it gets the same "abort while open" handling at its deadline
(see *`abort-transaction`* above). If nobody taps, the abort at the deadline gets `204` and the
terminal shows its red "failed" circle.
*Handled:* "the reversal never happened" is concluded only after the terminal's longest
transaction time (180 s), not after 30 s.
❓ Is the spec's REFUND transaction (`process-transaction` with `transactionType: REFUND`,
which the service uses after an abort) the same thing? It's untested live.

**any transaction can be reversed, not only the last.** ✅
A purchase was reversed even when it was not the terminal's latest transaction. The API contract's
phrase "reverse-last-transaction capability" doesn't hold as a restriction here.

---

## Behaviour staging doesn't show

What the spec describes but staging never produced in these experiments. Each is handled in the
code and covered by tests.

| What | How the service handles it |
|---|---|
| `PARTIAL` status | Never seen. The code reverses it at once and reports `declined`; only covered by tests. |
| `process-transaction` with `transactionType: REFUND` | The service's code uses it after an abort. It's unknown whether it's the same thing as `cancel-transaction` (see the reversal finding). |
| Re-sending a completed `ecrTransactionId` | Does MarketPay deduplicate, reject, or **charge again**? It's unsafe to test casually. The service's code never re-sends anything that may have arrived. |
| `cancellationResult` in `last-transaction` | The spec has it; staging never returned it. Does it ever appear? |
| `IN_PROGRESS` in `last-transaction` | The spec has it; never observed. |
| Terminal offline mid-transaction | What do `last-transaction` and abort answer then? |
| Notifications for a bank decline, a reversal, and progress | Final notifications were captured for an approval and a card timeout. A decline or a reversal is read by the same rules as its last-transaction record; progress is only logged. |

## Notifications (bonus webhook)

**a final notification, as staging sends it.** ✅ (three: approved and no card after a `202`, approved after a `201`)
Captured final notifications include an approved payment and a card-timeout payment. Both
`process-transaction` calls answered **`202`** first, as the spec says; the final result then came
only as the notification. Bodies:
[examples › Notifications](examples/marketpay-api.md#notifications-the-bonus-webhook). They match
the guide except:
- the envelope's `terminalTransactionId` is **`null`**; the result has its own terminal transaction id;
- the result **has `finalTransactionParams`** (the guide's example has none), with the same
  `ecrTransactionId` as the envelope;
- there, `amount` and `currency` are **numbers** (`100`, `752`), while `last-transaction` sends
  strings. *Was a bug:* the service's models accepted strings only, so this notification would
  have been ignored as unreadable (a safe failure: polling decides). They now accept both;
- the type is echoed as **`type": "PURCHASE"`**, never as `transactionType`; the service doesn't
  read it;
- also `mode: "DIRECT"`, `cashierId: "cloud"`, `result.terminalId` (an internal id, ignored);
- on the NOK, `amount`/`currency` are the real ones (`100`, `752`), unlike `last-transaction`'s
  NOK, which echoes `"0"`. The service never reads them.
Both read exactly like the same record from `last-transaction`: `approved`, and `failed` /
`terminal_stopped`; both are test fixtures. Still unknown: a bank decline's shape, a
reversal's, and whether a final notification also follows a request that broke (the service's crash).
It does follow a `201` (below).

**no progress notifications.** ✅ ❓ (two transactions)
The captured transactions did not produce `WAITING_FOR_CARD`, `PIN_REQUIRED` or
`BANK_AUTHORIZATION`, though the guide says they are sent for every transaction. Only the final one
arrived. *Impact:* none; the service only logs progress.

**a final notification follows a `201` too.** ✅
Not only after a `202`, as the spec says ("for asynchronous (202) transactions"). A `201` approved
payment also got a final notification, **identical in shape** to the approval above except for the
transaction-specific values. So a notification can't tell which way the request was answered, and
needn't. *Impact, good:*
the brief's worst case, "the money moved and the reply was lost", now has a second channel: a
lost `201` still arrives as a notification, and the request, polling by then, settles from it at
once even while `last-transaction` fails. *Impact, neutral:* every normal payment also gets a
notification after it is recorded: `already_settled`, one Firestore read, and a free
cross-check (`notification_contradicts_record` if it disagrees). A notification that overtakes
the HTTP answer is parked in the inbox; the request records the `201` as usual.
*Was a bug:* that cross-check counted a PARTIAL as "not charged", so a PARTIAL's notification
arriving after its reversal would have logged a false error. Fixed.

**the sender address looked cloud-hosted.** ✅ ❓
The sender address for a captured notification looked like a cloud-provider address. An IP
allowlist is only sound if MarketPay confirms a fixed set of sender addresses: a cloud address may
change.

What the guide (§3) and the spec promise, which the code relies on
([flows §9](flows.md#9-marketpay-notifications-webhook)):
- **Progress** notifications (`WAITING_FOR_CARD`, `PIN_REQUIRED`, `BANK_AUTHORIZATION`) for
  every transaction; the **final** one (`COMPLETED` with `result`) only for a transaction that
  answered `202`.
- Sent **once, never retried**; MarketPay never checks whether the service's URL is reachable.
  Polling stays the fallback, and must give the same result.
- **Not authenticated.** The service's only protection is the URL: each carries an HMAC of the
  payment id and operation under `NOTIFICATION_SECRET` (never logged; access logs mask it). ⚠️ That proves
  only that the sender knows the URL. Anyone who learns one (a log, a proxy) can forge that one
  operation's result. Before production: an **IP allowlist** of MarketPay's senders, and any
  signature or mTLS MarketPay can offer. The experiment logs each sender's address and
  header names to find out what's possible.
- The service's design rarely produces a `202`: it holds `process-transaction` open past its
  deadline so an abort still works. But the final notification also follows a `201` (see below), so
  it arrives on the normal path too, just after the HTTP answer: a free cross-check.

## Questions and known issues

The questions this raises for MarketPay, and the assumptions the service relies on meanwhile, are in
[open-questions.md](open-questions.md).
