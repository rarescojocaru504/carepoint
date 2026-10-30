# CarePoint, explained: a walkthrough for presenting the app

This is a plain-language tour of CarePoint for its owner. It follows one customer from opening the app to walking away with the item, then looks at what happens when things go wrong.

At each step you'll find:
- **what the customer sees**;
- **what happens behind the scenes**;
- **where it lives in the code**, so you can open the file and point at it.

You don't need to read code to follow it. The file paths are there for when someone asks "and where does that happen?".

**The cast:**

| Who | What it is | Where |
|---|---|---|
| **The app** | the customer's phone app (Expo / React Native) | `mobile/` |
| **The backend** | the server everything talks to (Python, FastAPI) | `backend/app/` |
| **PostgreSQL** | the database: the permanent record of users, machines, stock, reservations and orders | Docker container `db` |
| **Redis** | a very fast in-memory store, used as the "gate" that decides who gets the last unit, and to broadcast live updates | Docker container `redis` |
| **The machine** | today a simulator playing six machines; tomorrow a Raspberry Pi in a real cabinet | `hardware_sim/` |
| **The kiosk** | the touchscreen on the machine where you type your code | `hardware_sim/kiosk/` |
| **The dashboard** | the operator's web console | `dashboard/` |

---

## Part 1: The happy path

### 1. Opening the app

**The customer sees:** a spinner for a moment, then the map. If they're new, they see the sign-in screen first, where they can also create an account.

**Behind the scenes:**
- **Signing in again silently.** The app keeps a long-lived "refresh token" in the phone's secure keystore (the iPhone Keychain or Android Keystore). On start it trades it for a short-lived 15-minute "access token" that's kept only in memory.
- **No signal is not "signed out".** If the phone has no signal, the app says "Can't reach CarePoint". It doesn't sign the customer out.
- **The live connection.** Once signed in, the app opens a live connection to the server, so anything that changes (stock, their reservation, a machine going offline) arrives within a second.

**In the code:**
- `mobile/App.tsx`: decides what to show (spinner, sign-in, or the main screens).
- `mobile/src/store/auth.ts`: the signed-in state; `restore()` runs at start.
- `mobile/src/services/tokens.ts`: where tokens are kept: the access token in memory, the refresh token in SecureStore.
- `mobile/src/services/api.ts`: every call to the server; renews the token once when it expires.
- `backend/app/api/v1/auth.py` and `backend/app/services/auth.py`: sign-in, registration, token renewal. Passwords are stored as argon2 hashes. A stolen, re-used refresh token logs out every session that came from that sign-in.
- **The live connection:**
  - `mobile/src/components/LiveUpdates.tsx` (when to connect);
  - `mobile/src/services/live.ts` (the connection itself);
  - `backend/app/api/v1/events.py` (the server's end).

### 2. Finding a machine

**The customer sees:** a map with pins and, below it, the machines sorted by distance. Each one shows its distance, how many items it has, and whether it's open, sold out, offline or in maintenance.

**Behind the scenes:**
- **Where to look from.** The app asks for the phone's location; if refused, or if nothing is within 20 km, it uses the Bucharest demo area.
- **Nearest first.** The server finds the machines near that point with a geographic query (PostGIS) and sorts them by distance.
- **What "available" means.** It counts available units as *what's physically in the machine minus what other people are holding right now*. A unit someone reserved a minute ago isn't offered to anyone else.

**In the code:**
- `mobile/src/screens/MapScreen.tsx` (the screen);
- `mobile/src/store/machines.ts` (loads the machines);
- `mobile/src/services/location.ts` (the phone's location or the demo area);
- `mobile/src/machineState.ts` (how a machine is labelled and coloured);
- `backend/app/api/v1/discovery.py` and `backend/app/services/discovery.py` (the nearby search);
- `backend/app/services/availability.py` (the one definition of "available").

### 3. Looking inside a machine

**The customer sees:** the machine's products with price, "N left", "Low stock" or "Sold out", and a **Reserve** button. If the machine is offline or in maintenance, a warning appears and Reserve is greyed out.

**In the code:** `mobile/src/screens/MachineScreen.tsx`, and `GET /api/v1/machines/{id}` in `backend/app/api/v1/discovery.py`.

### 4. Reserving: the heart of CarePoint

**The customer sees:** they tap **Reserve** and land on "Your reservation" with **Held for you** and a countdown from 15:00.

**Behind the scenes:** this is the part that must never go wrong. If three people tap Reserve on the last unit at the same instant, exactly one may get it.
1. **A key per tap.** The app sends the request with a unique key per tap, so if the phone retries because the network hiccupped, the customer doesn't end up holding two units.
2. **The atomic gate.** The server asks **Redis** to place a hold with a small script that runs atomically: "check what's available, and if there's enough, record this hold with its expiry time". Nothing can sneak in between the check and the hold.
3. **The permanent record.** Only then does it write the reservation to **PostgreSQL**, with status `held` and an expiry time.
4. **Telling everyone.** It announces the change: the customer's app hears "your reservation was created"; everyone else hears only "availability changed at this machine", never whose reservation it was.

This has been load-tested: 500 simulated people raced for one last unit, ten times over, and exactly one won every time ([`docs/benchmarks.md`](../docs/benchmarks.md)).

**In the code:**
- `mobile/src/store/reservations.ts`: `reserve()` and the per-tap key.
- `backend/app/api/v1/reservations.py`: the endpoint.
- `backend/app/services/reservations.py`: `create_reservation`, including the per-customer limit (3 active holds).
- `backend/app/services/hold_store.py`: **the Redis scripts, the gate itself**. ADR-012 explains why expiry is part of the arithmetic: a hold stops counting the moment it expires, even if every background job is down.
- `backend/app/services/events.py`: the announcements, and what customers are allowed to see (ADR-026).

### 5. The countdown

**The customer sees:** the countdown ticking down. It turns red in the last two minutes.

**Behind the scenes:** the countdown follows the **server's** clock, not the phone's. Every response from the server carries the time, and the app works out the difference. A phone whose clock is wrong still shows the right time left.

**In the code:** `mobile/src/components/Countdown.tsx` and `mobile/src/services/clock.ts`.

### 6. Paying

**The customer sees:** **Pay 12.99 RON**. After tapping it: a **QR code** and a big **6-digit code**, with "Pick up within" and the countdown.

**Behind the scenes:**
- **Authorise, don't charge.** The payment is *authorised*, not charged: the money is reserved on the card. It's taken only when the machine hands over the item. If the hold expires or the customer cancels, the authorisation is *voided* and nothing is taken. This is the same approach as hotels and car rentals.
- **A mock provider for now.** Payments use a mock provider for the MVP. A real one (for example Stripe or Netopia) plugs in behind the same interface.
- **The pickup code** is random. The server keeps only a keyed fingerprint (a hash) of it, mixed with the machine's identity, so the code works **only at that one machine**, and only while the reservation is paid and unexpired. The app keeps the code in memory and **never writes it to the phone's storage**. If the app is restarted, "Show my pickup code" makes a new one, and the old one stops working.

**In the code:**
- `mobile/src/screens/ReservationScreen.tsx` (the screen) and `mobile/src/components/PickupCode.tsx` (QR + code).
- `backend/app/services/checkout.py`: held → paid, under a database lock, so a double-tap can't pay twice.
- `backend/app/services/payments.py`: the payment interface and the mock.
- `backend/app/services/pickup_codes.py`: making, storing (as a hash) and checking codes; wrong guesses lock the machine for a while.

### 7. At the machine

**The customer sees:** on the machine's touchscreen they type the 6 digits, or show the QR code to its camera. The screen says the item is coming out, then **Please take your item**. Their phone switches to **Picked up**, by itself.

**Behind the scenes:** a careful exchange between machine and server.
1. **The machine asks.** The kiosk hands the code to its machine controller, which asks the server "is this code valid here?". The machine is connected to the server over a permanent, encrypted connection (a WebSocket). It proved who it is when connecting by signing with its own private key, which never leaves the machine.
2. **The server checks and commands.** It checks the code against the stored fingerprint for *this* machine. If valid, it extends the hold, so it can't expire while the item comes out, and sends a numbered **dispense** command.
3. **The machine dispenses.** It saves the command number first, so it can never dispense the same command twice even after a crash. It turns the motor, then reports the result, and keeps that report until the server confirms receipt.
4. **The server completes the order.** It records the item leaving the stock ledger, marks the reservation **dispensed**, **captures** the payment, and announces it: the customer's app and the operator's dashboard update straight away.

**In the code:**
- **The kiosk:**
  - `hardware_sim/kiosk/src/Kiosk.tsx` and `components/Keypad.tsx` (the screen);
  - `hardware_sim/carepoint_sim/kiosk_api.py` (the screen ↔ machine link);
  - `hardware_sim/carepoint_sim/machine.py` (the simulated machine: connection, dispensing, reconnecting);
  - `hardware_sim/carepoint_sim/state.py` (what it saves to survive a crash).
- **The server:**
  - `backend/app/api/v1/machine_ws.py` (the connection);
  - `backend/app/services/machine_auth.py` (checking the machine's signature);
  - `backend/app/services/machine_gateway.py` (routing every message);
  - `backend/app/services/pickups.py` (`request_pickup`, `record_dispense_result`);
  - `backend/app/services/dispensing.py` (completing the order);
  - `backend/app/services/payment_settlement.py` (capture or void).
- **The rules a real machine must follow:** [`docs/machine-protocol.md`](../docs/machine-protocol.md), §6.

### 8. Afterwards

**The customer sees:** **My orders** lists the order as "Picked up · Charged". The operator's dashboard shows it in the live feed, and the KPI tiles (pickup rate, average time from hold to pickup) move.

**In the code:**
- `mobile/src/screens/OrdersScreen.tsx`;
- `backend/app/services/orders.py`;
- `dashboard/src/views/Reservations.tsx` (the live feed);
- `backend/app/services/kpis.py`.

---

## Part 2: When things go wrong, and how to show it

Every scenario below is handled by design and covered by automated tests. The [manual testing guide](../docs/manual-testing-guide.md) (section 7) has more of them.

### The hold expires: "I didn't make it in time"

**What happens:**
1. As soon as the countdown passes zero, the unit counts as available again. A background job, which runs every 15 seconds, then marks the reservation **expired** and **voids** the payment.
2. The customer's app switches to "This hold ran out… You haven't been charged" with **Reserve again**.
3. The dashboard shows the reservation as **expired · voided**.

Even if that background job stopped, no one could be blocked by a dead hold: an expired hold simply stops counting (ADR-012).

**In the code:**
- `backend/app/services/background.py` (the loop);
- `backend/app/services/reservation_expiry.py` (the sweeper);
- `backend/app/services/payment_settlement.py` (the void);
- the app reacts in `mobile/src/services/liveSync.ts`, and the screen in `ReservationScreen.tsx`.

**How to demo it:**
1. Switch to one-minute holds: `RESERVATION_HOLD_SECONDS=60 docker compose up -d --wait backend`.
2. Reserve and pay on the phone, then wait a minute.
3. Show the phone and the dashboard change on their own.

### A machine goes offline

**What happens:** within a couple of seconds:
- the dashboard shows the machine offline;
- in the app the pin turns red, **Reserve** is disabled, and a reservation at that machine shows a warning: the hold stays until the timer runs out, and the customer is only charged if the machine hands the item over;
- when it reconnects, everything turns back to normal.

**In the code:**
- `backend/app/services/machine_gateway.py`, `close_connection` (records the disconnection);
- `backend/app/services/machine_status.py` (what "online" means);
- in the app, the warning in `ReservationScreen.tsx` and the disabled button in `MachineScreen.tsx`.

**How to demo it:**
1. On the machine's kiosk, open ⚙ (bottom right) and tick **Offline**.
2. Show the dashboard and the phone.
3. Untick it. For a bigger effect, `docker compose stop simulator` takes all six machines offline; `docker compose start simulator` brings them back.

### The machine jams

**What happens:** the machine tries to dispense, but nothing comes out. It reports "failed, 0 units, jam".
- **Nothing came out:** the reservation **stays paid** and the customer can type the same code again. Nobody is charged for an item they didn't get.
- **Only part of an order came out** (a partial dispense): the order is **cancelled and not charged**, and the units that did come out are recorded.

**In the code:** `backend/app/services/pickups.py`, `record_dispense_result` (with `_record_partial` for partial dispenses), and the protocol's table of outcomes in [`docs/machine-protocol.md`](../docs/machine-protocol.md) (`dispense_result`).

**How to demo it:**
1. On the kiosk's ⚙ panel set **Jam** to **next dispense**.
2. Type a valid code. The kiosk reports a problem, and the app still shows the code.
3. Type it again. This time it works.

### Also handled

| Scenario | What happens | Where |
|---|---|---|
| **The connection drops right after dispensing** | the machine kept its result and sends it on reconnecting; the customer is charged exactly once | `hardware_sim/carepoint_sim/machine.py`, `state.py`; `backend/app/services/pickups.py` |
| **The phone loses signal** | "Reconnecting… live updates are paused" banner; it catches up when back online | `mobile/src/services/live.ts`, `components/LiveUpdates.tsx` |
| **Redis loses its data** | holds are rebuilt from PostgreSQL the first time a slot is touched; nothing oversells | `backend/app/services/hold_store.py`, `hold_reconciliation.py` |
| **Someone guesses codes at a machine** | after 20 wrong codes a minute, the machine refuses every code for a while | `backend/app/services/pickup_codes.py` |
| **Someone tries to read another customer's reservation** | 404, as if it didn't exist | every query in `backend/app/services/reservations.py` is filtered by owner |

---

## Part 3: Presenting it

- **Before:** follow the runbook in [`docs/pitch/README.md`](../docs/pitch/README.md): short holds, the demo script running, the dashboard and a kiosk open, the phone ready.
- **The story to tell:**
  1. find on the map;
  2. reserve (it's yours for 15 minutes);
  3. pay (authorised, not charged);
  4. code at the machine (the item comes out, now you're charged);
  5. or don't come (released, not charged).

  Then show one failure, offline or jam, and how everything reacts at once.
- **Questions you'll likely get:**
  - *"What if two people want the last one?"* Exactly one gets it; it's load-tested with 500 at once (`docs/benchmarks.md`).
  - *"Is it secure?"* Codes are stored only as fingerprints and work at one machine; machines sign their connections; customers only see their own reservations. There's a recorded whole-system security review (ADR-031).
  - *"Does it need a real machine?"* The protocol is written so a Raspberry Pi can replace the simulator without changing the server; parts and costs are in `docs/pitch/hardware-prototype.md`.
- **If the live demo fails:** play [`docs/pitch/demo-video.webm`](../docs/pitch/demo-video.webm) (65 s).
