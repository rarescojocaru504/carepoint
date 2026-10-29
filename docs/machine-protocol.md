# CarePoint machine protocol

**Version 1** (WebSocket subprotocol `carepoint.v1`), revision 4: the connection, telemetry, stock counts and pickups (§3–§6). A working reference client is the hardware simulator in [`hardware_sim/`](../hardware_sim), written from this document without any backend code.

This is the contract between the CarePoint backend and a vending machine. Anything that follows it can replace the simulator without any change to the backend: a Raspberry Pi, an ESP32 or a Python daemon. Every message is described here, and the exact JSON Schema of every message is in [`machine-protocol.schema.json`](machine-protocol.schema.json). That file is generated from the server's own validation models, and a test fails if the two drift apart. The design rationale is in ADR-010 and ADR-017 in [`architecture-decisions.md`](architecture-decisions.md).

## 1. Connecting

```
GET /api/v1/ws/machines
Upgrade: websocket
Sec-WebSocket-Protocol: carepoint.v1
X-CarePoint-Machine:   CP-BUC-0001
X-CarePoint-Timestamp: 1767225600
X-CarePoint-Nonce:     <22-64 base64url chars, fresh for every connection>
X-CarePoint-Signature: <Ed25519 signature, unpadded base64url>
```

- **Transport:** use `wss://` (TLS) in production. Local development uses `ws://localhost:8000`.
- **Subprotocol:** the machine must offer `carepoint.v1`, and the server confirms it in the response.
- **Authentication:** the four `X-CarePoint-*` headers use the same request-signing scheme as the machine's HTTP calls (ADR-010). For the handshake, sign:
  - method `GET`;
  - the path `/api/v1/ws/machines`;
  - an empty body.

  The signed text is these lines joined with `\n`, with no trailing newline:
  ```
  CAREPOINT-ED25519-V1
  GET
  /api/v1/ws/machines
  CP-BUC-0001
  1767225600
  <nonce>
  e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
  ```
  The last line is the SHA-256 of the empty body. ADR-010 has a known-answer test vector for checking an implementation.
- **Fresh signature every time:** each connection attempt needs a new nonce and the current time. The server accepts each nonce only once, and only within ±60 s of its own clock, so the machine must keep its clock in sync (NTP).

**Refused handshakes** get a plain HTTP response instead of an upgrade, with a JSON body `{"detail": "..."}`:

| Status | Meaning | What the machine should do |
|---|---|---|
| 400 | `carepoint.v1` was not offered | Fix the firmware |
| 401 | Bad, stale or replayed signature; unknown or decommissioned machine; no key registered | Check the clock and the key, then retry with backoff |
| 503 | The server can't check nonces right now | Retry with backoff |

The server never says which check failed; the reason is written to its log.

## 2. Messages in general

- **Frames:** every frame is a **text** frame holding exactly one JSON object, at most **16 KiB** encoded as UTF-8. A binary frame or a larger message closes the connection (§5).
- **Message type:** every message has a `type`.
- **Message id:** every message from the machine also has an `id` of 1–64 characters from `A-Z a-z 0-9 _ -`. The machine chooses it, and it should be unique on the connection (a counter is fine). The server echoes it as `reply_to`.
- **Exactly one reply:** every machine message gets one reply, in the order the messages were sent:
  - an `ack`, meaning it was applied;
  - a `verify_result`, for `verify_code`; or
  - an `error`, meaning nothing of it was applied (§4).
- **Server commands:** the server also sends **commands** of its own (`dispense`, `adjust_stock`). They have no `reply_to` and can arrive at any time, including straight after `welcome`.
- **Strict validation, no coercion:** the server rejects the message instead of guessing:
  - unknown fields;
  - a string where a number is expected (`"5"`), or `true` for a number;
  - a fraction where an integer is expected;
  - `NaN`, or any value outside its range.
- **Times** are ISO 8601 with a time zone, for example `2026-10-29T09:30:00.123456Z`.

## 3. Message reference

### Server → machine: `welcome`

This is sent once, straight after the handshake.

```json
{"type": "welcome", "protocol": 1, "machine_id": "6f1c…", "serial_number": "CP-BUC-0001",
 "server_time": "2026-10-29T09:30:00.123456Z", "heartbeat_interval_seconds": 30,
 "slots": [{"slot": "A1", "product_sku": "PAD-REG-10", "product_name": "Sanitary pads (10)",
            "capacity": 10, "quantity": 8},
           {"slot": "B4", "product_sku": null, "product_name": null, "capacity": 10, "quantity": 0}]}
```

The machine should compare `server_time` with its own clock, and send a heartbeat every `heartbeat_interval_seconds`.

`slots` is the **planogram**: which product belongs in which slot, as operators configured it. A slot with `product_sku: null` must stay empty. `quantity` is what the server *believes* is in the slot:
- **First start:** a machine with no counts of its own (fresh out of the box) may adopt it.
- **After that:** a machine reports what it physically counts (`stock_report`), never echoes this number back.

### Machine → server: `heartbeat`

```json
{"type": "heartbeat", "id": "42", "firmware_version": "1.4.0", "uptime_seconds": 86400}
```

| Field | Rule |
|---|---|
| `firmware_version` | 1–32 characters from `A-Z a-z 0-9 . _ + -` |
| `uptime_seconds` | integer, 0 to 315 360 000 (10 years) |

- **Online status:** the server records the time it was last heard from (`last_seen_at`). A machine counts as **online** in the app while it was heard from within the last 90 s.
- **Missing heartbeats:** at the default interval of 30 s, the machine stays online across one lost heartbeat.
- **Silence:** a connection that sends nothing at all for 90 s is closed (§5).

Reply: `ack`.

### Machine → server: `telemetry`

```json
{"type": "telemetry", "id": "43", "door": "closed", "temperature_c": 21.5}
```

| Field | Rule |
|---|---|
| `door` | `"open"` or `"closed"` (the service door) |
| `temperature_c` | number from −40 to 85; it is stored to one decimal place |

Send it whenever a reading changes, and at least once per heartbeat interval. The latest values are kept, and every change of door state is logged as an event for the operator dashboard. Reply: `ack`.

### Machine → server: `stock_report`

```json
{"type": "stock_report", "id": "44",
 "slots": [{"slot": "A1", "quantity": 7}, {"slot": "A2", "quantity": 0}]}
```

| Field | Rule |
|---|---|
| `slots` | 1–64 entries; each slot may appear only once |
| `slot` | a slot code of this machine: a capital letter and 1–2 digits, such as `A1` or `B12` |
| `quantity` | integer, 0 or more: the units physically in the slot **right now** |

What the server does with it:
- **The machine's count is the truth:** it is the only witness of what is physically inside it.
- **Mismatches are corrected:** for each listed slot whose count differs from the database, the server corrects the database to the reported count. Each correction is written to the stock ledger as a `telemetry_correction`, with a note such as `machine counted 3, database had 5`.
- **Matching counts:** a slot whose count matches changes nothing and writes nothing.
- **Partial reports:** slots that aren't listed are left alone, so a machine can report one slot at a time.

A slot with a dispense **in progress** (a `dispense` sent but no result received yet) is left alone even if its count differs. Its count is changing, and the `dispense_result` accounts for those units, so the server doesn't correct it ahead of the result.

When to send it:
- after connecting, and after any stored `dispense_result`s (§6);
- after a restock;
- whenever its own count changes for a reason other than a dispense, for example an operator's adjustment.

The **whole report is rejected**, and nothing of it applied, if any entry:
- names a slot this machine doesn't have (`unknown_slot`);
- exceeds the slot's capacity (`out_of_range`);
- reports items in a slot that has no product assigned (`slot_not_configured`).

Reply: `ack`, or one of those `error`s.

### Machine → server: `error_event`

```json
{"type": "error_event", "id": "45", "code": "jam", "slot": "A1", "message": "motor stalled"}
```

| Field | Rule |
|---|---|
| `code` | `jam`, `motor_fault`, `sensor_fault`, `door_forced`, `temperature_out_of_range`, `power_loss` or `other` |
| `slot` | optional: the slot code the fault concerns |
| `message` | optional: 1–200 characters, no control characters (no newlines) |

The event is logged for the operator dashboard. Reply: `ack`.

### Machine → server: `verify_code`

```json
{"type": "verify_code", "id": "46", "credential": "482913"}
```

| Field | Rule |
|---|---|
| `credential` | 1–64 printable ASCII characters: what the customer typed (6 digits) or what the camera read from their QR code (`CP1:<reservation id>:<code>`), passed on unchanged |

Reply: `verify_result`. If it is `ok`, a `dispense` command follows straight away.

### Server → machine: `verify_result`

```json
{"type": "verify_result", "reply_to": "46", "ok": true, "refusal": null,
 "retry_after_seconds": null, "command_id": "9b2e…"}
```

| `refusal` | Meaning | What the kiosk should show |
|---|---|---|
| `invalid_code` | No paid, unexpired order with this code at **this** machine | "Code not recognised" |
| `too_many_attempts` | Too many wrong codes at this machine; even a correct one waits. `retry_after_seconds` says how long | "Please wait N seconds" |
| `pickup_in_progress` | This order is already being dispensed | Keep showing the dispense in progress |
| `machine_unavailable` | The machine is in maintenance | "Out of service" |

- **Wrong codes don't count against the connection:** they are the customer's mistake, not the machine's, so they never count towards the connection's invalid-message limit.
- **Guessing is limited per machine:** guessing is held back by a per-machine lockout instead (ADR-016).

### Server → machine: `dispense` (command)

```json
{"type": "dispense", "command_id": "9b2e…", "reservation_id": "c41a…", "slot": "A1", "quantity": 1}
```

Release `quantity` units from `slot`, then report what happened with `dispense_result`. The rules in §6 are what make a dispense safe across disconnects.

### Server → machine: `adjust_stock` (command)

```json
{"type": "adjust_stock", "command_id": "5d0e…", "slot": "A1", "delta": 6, "reason": "restock"}
```

An operator recorded in the dashboard that they put units into the slot (`restock`, `delta` > 0) or took some out (`adjustment`, either sign). The server already counts them, so:
- **Update the count:** add `delta` to your own count for the slot, at most once per `command_id`. Never go below 0 or above the slot's capacity.
- **Confirm:** send a `stock_report` for the slot.

A machine with sensors that sees the change by itself just sends the `stock_report`. Operators can only record a change while the machine is online, because a machine that never heard of it would undo it with its next stock report.

### Machine → server: `dispense_result`

```json
{"type": "dispense_result", "id": "47", "command_id": "9b2e…", "outcome": "dispensed", "units_dispensed": 1}
{"type": "dispense_result", "id": "48", "command_id": "9b2e…", "outcome": "failed", "units_dispensed": 0, "error_code": "jam"}
```

| Field | Rule |
|---|---|
| `command_id` | the `command_id` of the `dispense`, issued to **this** machine |
| `outcome` | `"dispensed"` when every unit came out, otherwise `"failed"` |
| `units_dispensed` | integer, 0–10: the units that physically came out. It must equal the command's `quantity` for `dispensed` and be lower than it for `failed` |
| `error_code` | required when `failed`, and absent when `dispensed`; same values as `error_event` |

What the server does:

| Result | Order | Stock |
|---|---|---|
| `dispensed` | Completed and **charged** | Units leave the ledger |
| `failed`, 0 units | Stays paid. The customer can enter the same code again until the order expires; if it expires, the payment is voided | Unchanged |
| `failed`, some units | **Cancelled and not charged**: an incomplete order is never charged | The units that came out leave the ledger |
| Any result that arrives after the order expired (§6) | Not charged | The units that came out leave the ledger |

Reply: `ack`. The same result sent again for a finished command is also acknowledged and changes nothing. A result that contradicts the command (for example `dispensed` with fewer units than ordered) gets `out_of_range`. An unknown command id, or another machine's, gets `unknown_command`.

### Server → machine: `ack`

```json
{"type": "ack", "reply_to": "42"}
```

### Server → machine: `error`

```json
{"type": "error", "reply_to": "44", "code": "unknown_slot", "detail": "no such slot: Z9"}
```

`reply_to` is `null` when the message had no readable `id`, for example when it wasn't JSON. `detail` is for logs and humans; machines should branch on `code` (§4).

## 4. Error codes

| Code | Meaning | Retry? |
|---|---|---|
| `invalid_message` | Not JSON, an unknown `type`, or a field missing, unknown, of the wrong type or out of range. `detail` names the field | No: it is a firmware bug |
| `unknown_slot` | A stock report names a slot this machine doesn't have | No: check the slot configuration |
| `out_of_range` | A stock count exceeds the slot's capacity | No: recount |
| `slot_not_configured` | Items reported in a slot that has no product assigned | No: an operator must assign a product |
| `unknown_command` | A `dispense_result` for a command this machine was never sent | No: drop that stored result |
| `internal_error` | The server failed (database or Redis trouble), not the machine | **Yes**: resend the same message later |

Each rejected message, except `internal_error`, counts towards a limit of **10 per connection**. After that the server closes the connection (§5).

## 5. Connection lifetime

- **Keep-alive:** the server sends WebSocket pings every 20 s and drops a connection that doesn't answer within 20 s. Standard WebSocket libraries answer pings automatically.
- **One connection per machine:** a new connection from the same machine **supersedes** the old one. The old one is closed the next time it sends anything, so a machine that reconnected before noticing its old socket had died is never served twice.
- **Message limit:** at most **30 messages per 10 s** per connection.
- **Reconnecting:** after any close or network failure, reconnect with exponential backoff:
  - start at 1 s, double each time, up to 60 s;
  - add random jitter;
  - use a new nonce and timestamp every time.

**Close codes sent by the server:**

| Code | Reason | What the machine should do |
|---|---|---|
| 1003 | A binary frame was sent | Fix the firmware; reconnect |
| 1008 | Over the message rate, or too many rejected messages | Fix the firmware; reconnect with backoff |
| 1009 | A message was over 16 KiB | Fix the firmware; reconnect |
| 1011 / 1012 | Server error or restart | Reconnect with backoff |
| 4000 | Superseded by a newer connection from this machine | Nothing: the newer connection is the live one |
| 4003 | The machine was decommissioned by an operator | Stop reconnecting and show "out of service" |
| 4008 | Nothing received within the online window (90 s) | Reconnect, and send heartbeats on time |

## 6. Dispensing safely

A dispense is the only operation that moves goods and money, so a machine **must** follow these four rules. The server is built around them; ADR-018 explains the design.

1. **Execute a `command_id` at most once.** Store the command id durably *before* dispensing. If the same `dispense` arrives again (the server resends open commands after every reconnect, because it can't know what you received), **don't dispense again**: send that command's stored `dispense_result` once more. If a dispense was interrupted, for example by a power cut, don't restart it; report it as `failed` with the units that actually came out.
2. **Keep every `dispense_result` until it is acknowledged.** Store it before sending, and delete it only once the `ack` for it arrives. Storage must survive a reboot.
3. **After (re)connecting, send stored results first**, before any `stock_report`, so the count you report already includes them.
4. **Count honestly.** Report the units that physically left, even after a fault. A partial dispense is `failed` with the true `units_dispensed`.

What the server guarantees in return:
- **The hold outlasts the dispense:** when a code is accepted, the order's hold is extended to at least **120 s** from that moment (`DISPENSE_TIMEOUT_SECONDS`). A slow dispense can't race the expiry, and the units can't be sold to someone else while they are coming out.
- **One dispense at a time:** while a dispense is open, the order can't be cancelled from the app, and the code returns `pickup_in_progress`.
- **Resending is limited:** an open `dispense` is resent after a reconnect only while its order is still valid. After that nobody is waiting at the machine, and resending would hand an item to whoever walks by.
- **Late results are still recorded:** if no result arrives before the extended hold ends, the order expires, its payment is voided and the command is marked abandoned. A result that arrives later is still recorded, so stock stays correct, but nobody is charged.

**Disconnect in the middle of a dispense:**

```
machine                                   server
   |-- verify_code -------------------------->|  code ok, hold extended, command open
   |<-------------- verify_result (ok) -------|
   |<-------------- dispense (C1) ------------|
   |   (dispenses, stores result for C1)      |
   X  connection lost before sending it       |
   |-- new signed handshake ----------------->|
   |<-------------- welcome ------------------|
   |<-------------- dispense (C1)  (resent) --|  C1 is still open
   |-- dispense_result (C1, from storage) --->|  order completed, payment captured
   |<-------------- ack ----------------------|
   |-- dispense_result (C1)  (answer to the   |
   |   resent C1: not dispensed again) ------>|  already applied: no change
   |<-------------- ack ----------------------|
   |-- stock_report ------------------------->|
```

## 7. A typical session

```
machine                                             server
   |-- GET /api/v1/ws/machines (signed, carepoint.v1) -->|
   |<---------------------------- 101 Switching Protocols|
   |<-------------------- {"type":"welcome",...}         |
   |-- {"type":"stock_report","id":"1","slots":[...]} -->|
   |<-------------------- {"type":"ack","reply_to":"1"}  |
   |-- {"type":"telemetry","id":"2",...} --------------->|
   |<-------------------- {"type":"ack","reply_to":"2"}  |
   |-- {"type":"heartbeat","id":"3",...}  (every 30 s) ->|
   |<-------------------- {"type":"ack","reply_to":"3"}  |
   |-- {"type":"verify_code","id":"4",...} ------------->|  customer typed a code
   |<-- {"type":"verify_result","reply_to":"4","ok":true}|
   |<-- {"type":"dispense","command_id":"C1",...}        |
   |-- {"type":"dispense_result","id":"5",...} --------->|
   |<-------------------- {"type":"ack","reply_to":"5"}  |
```

## Changelog

| Revision | Date | Change |
|---|---|---|
| 1 | 2026-10-29 | Handshake, `welcome`, `heartbeat`, `telemetry`, `stock_report`, `error_event`, `ack`, `error`, close codes |
| 2 | 2026-10-29 | Pickups: `verify_code`, `verify_result`, `dispense`, `dispense_result`, the `unknown_command` error, §6 dispensing rules; `stock_report` leaves slots mid-dispense alone |
| 3 | 2026-10-29 | `welcome.slots`: the planogram, so a machine needs no local slot configuration |
| 4 | 2026-10-29 | `adjust_stock`: restocks and adjustments recorded by operators; a clean disconnect now shows the machine offline at once |
