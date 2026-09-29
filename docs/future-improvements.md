# Future improvements

Ideas for taking the service further, beyond what the assignment asks. None of them is built.
Each says what it would solve, how it could work, and what to watch out for.

## What any change must keep

These are the reasons the service is correct. An improvement that weakens one of them is not
an improvement, however much it simplifies something else.

- **The POS gets a definitive answer within its deadline.** A waiter who hears nothing runs the
  card again.
- **Only MarketPay's own record decides an outcome.** A timeout or a `202` is never one.
- **One operation per terminal at a time.** MarketPay only reports a terminal's *last*
  transaction, so a second one would erase the first one's evidence.
- **Nothing that may have reached MarketPay is sent again.**
- **Every state change is atomic with its terminal lock** (and now its history entry).

## 1. Message queues

A queue is a good fit for work that can happen **after** the POS has its answer, or that must
survive a restart. It is a poor fit for the payment itself: the brief requires a synchronous,
definitive answer, and the customer is standing at the terminal. So a queue would sit next to
the synchronous path, not replace it.

### 1.1 Durable notification intake

**Today:** the webhook answers MarketPay `200` at once and hands the notification to an
in-process worker thread. If the process dies in between, that notification is gone, and
polling settles the payment instead (correct, only slower).

**With a queue:** the webhook publishes the raw notification to a queue (e.g. Google Pub/Sub)
and answers `200`. A consumer applies it with the same rules as today, and acknowledges only
after the store write committed. A crash just means redelivery. That is safe, because applying
the same record twice changes nothing.

**Watch out for:** ordering between notifications for one payment (use an ordering key such as
the payment id), and a dead-letter queue for notifications that keep failing.

### 1.2 An outbox of payment events

**Today:** nothing outside the service learns about a change unless it asks.

**With a queue:** every state change also writes an event (`payment.approved`,
`payment.cancelled`, …) to an `outbox` collection in the **same** Firestore transaction. That's
the same technique as the payment history. A relay publishes the outbox to a queue for
accounting, the POS back office, analytics or alerting. This is the *transactional outbox*
pattern: no event for a change that didn't commit, and none lost for one that did.

**Watch out for:** consumers must be idempotent (an event can be delivered twice), and the relay
must mark events as sent without losing any across its own restarts.

### 1.3 A work queue for payments that need a person

**Today:** the few cases a person must settle are visible only through reads and logs: a
`PARTIAL` that couldn't be reversed, a late charge found after the service reported `failed`,
a notification that contradicts a settled record.

**With a queue:** each such case becomes a task in an operator queue (or a ticket), carrying
the payment id and its history. It's closed when a person has acted on it.

### 1.4 An asynchronous mode for POS systems that can take it

**Today:** everything relies on the POS waiting longer than the deadline. A POS with a shorter
timeout risks the waiter running the card again.

**With a queue:** `POST /payments` could offer an opt-in asynchronous mode. It would answer `202`
with the payment id at once, and push the final outcome to the POS by callback or push channel
(fed by the outbox, §1.2), with `GET /payments/{id}` as the fallback. The terminal-side flow
stays exactly as it is.

**Watch out for:** it only helps a POS that is built for it (it must show "waiting for the card"
and wait for the push). The synchronous mode stays the default.

### 1.5 Per-terminal ordering through a queue, instead of the lock

**Idea:** route every operation for a terminal through a queue partition keyed by terminal id,
so they're processed strictly one at a time.

**Why not to rush it:** it serialises the *requests*, but the evidence problem remains. The
terminal's last record must stay the payment's own until its outcome is confirmed, including
after a crash, and a queue doesn't hold that state. The Firestore lock does, atomically with the
payment. A queue could complement the lock (fewer `409 terminal_busy` answers at a busy table),
but not replace it.


## 2. Resilience and security

- **A circuit breaker for MarketPay outages:** when MarketPay is clearly down, answer quickly
  with a clear error, instead of every POS waiting out its deadline. Only for calls that can't
  have started a transaction.
- **Authenticated notifications:** an IP allowlist of MarketPay's senders, or a signature check
  if MarketPay offers one, on top of the signed URLs.
- **Certificate rotation:** the mTLS certificate expires. Load it from a secret manager, alert
  before it expires, and reload it without a restart.
- **Authentication of the POS** (out of scope for the assignment): per-restaurant credentials,
  and a per-terminal authorisation check.

## 3. MarketPay integration

- **An anti-corruption layer:** today the domain reads MarketPay's own payload models (in DDD
  terms, the service conforms to its model). Translating them at the border into the service's
  own terminal-record type would make the domain independent of MarketPay's formats, at the cost
  of a mapping layer.
- **Confirm the open questions with MarketPay** (see [open-questions.md](open-questions.md)),
  e.g. whether `ecrTransactionId` deduplicates, and how to authenticate notifications. Some
  answers would let the service simplify: if a lost request could be re-sent safely, a few recovery
  paths would shorten.
