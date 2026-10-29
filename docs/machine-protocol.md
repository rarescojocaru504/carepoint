# CarePoint machine protocol

**Version 1** (WebSocket subprotocol `carepoint.v1`). Revision 1 covers the connection, telemetry and stock counts. The pickup commands (`verify_code`, `dispense`, `dispense_result`) are added in revision 2, together with the rest of Phase 6.

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
  - an `ack`, meaning it was applied; or
  - an `error`, meaning nothing of it was applied (§4).
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
 "server_time": "2026-10-29T09:30:00.123456Z", "heartbeat_interval_seconds": 30}
```

The machine should compare `server_time` with its own clock, and send a heartbeat every `heartbeat_interval_seconds`.

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

When to send it:
- after connecting;
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

## 6. A typical session

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
```

## Changelog

| Revision | Date | Change |
|---|---|---|
| 1 | 2026-10-29 | Handshake, `welcome`, `heartbeat`, `telemetry`, `stock_report`, `error_event`, `ack`, `error`, close codes |
