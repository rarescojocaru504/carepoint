# Manual testing guide

This guide walks you through running CarePoint and trying every feature by hand: the API, the operator dashboard, the machine screens (kiosks), the customer app on your phone, and the failure scenarios. Each step says what to do and **what you should see**.

You don't need to read any code. If something doesn't match what this guide describes, that's worth reporting.

**Contents**
1. [Start the system](#1-start-the-system)
2. [How the pieces fit together](#2-how-the-pieces-fit-together)
3. [The API in Swagger](#3-the-api-in-swagger)
4. [The operator dashboard](#4-the-operator-dashboard)
5. [The machine screens (kiosks)](#5-the-machine-screens-kiosks)
6. [The customer app on your phone](#6-the-customer-app-on-your-phone)
7. [Failure scenarios](#7-failure-scenarios)
8. [Logs and metrics](#8-logs-and-metrics)
9. [The automated tests](#9-the-automated-tests)
10. [Reset and troubleshooting](#10-reset-and-troubleshooting)

---

## 1. Start the system

You need **Docker Desktop** running. For section 6 you also need **Node.js 22** and the **Expo Go** app on your phone.

From the repository folder:

```bash
docker compose up --build --wait
```

**You should see:** after a few minutes, every container reports `Healthy` and the command returns.

| Open | What it is |
|---|---|
| http://localhost:8000/docs | **Swagger**: the API, with a "Try it out" button on every endpoint |
| http://localhost:3000 | **Operator dashboard** |
| http://localhost:8080 | **Kiosks**: the touchscreens of the six simulated machines |

| Account | Email | Password |
|---|---|---|
| Customer | `customer@carepoint.demo` | `carepoint-customer-demo` |
| Operator | `operator@carepoint.demo` | `carepoint-operator-demo` |
| Admin | `admin@carepoint.demo` | `carepoint-admin-demo` |

The six demo machines, all in Bucharest:

| Serial | Name | Note |
|---|---|---|
| CP-BUC-0001 | Universitate - Piata Universitatii | |
| CP-BUC-0002 | Politehnica - Rectorat | |
| CP-BUC-0003 | ASE - Piata Romana | |
| CP-BUC-0004 | Campus Regie - Camin C1 | |
| CP-BUC-0005 | Gara de Nord - Hol principal | dispenses slowly on purpose (4 s per item; the others take 1.5 s) |
| CP-BUC-0006 | Medicina - UMF Carol Davila | **in maintenance** on purpose: nothing can be reserved there |

**Demo mode (short holds).** A hold normally lasts 15 minutes. To see expiries without waiting, restart just the backend with one-minute holds:

```bash
RESERVATION_HOLD_SECONDS=60 docker compose up -d --wait backend
```

To go back to 15 minutes, run `docker compose up -d --wait backend`. In PowerShell, the short-hold command is:

```powershell
$env:RESERVATION_HOLD_SECONDS=60; docker compose up -d --wait backend; Remove-Item Env:RESERVATION_HOLD_SECONDS
```

---

## 2. How the pieces fit together

```
 Phone app ──REST + live notices──▶ ┌──────────────┐ ──SQL──▶ PostgreSQL (the truth)
 Dashboard ──REST + live notices──▶ │   Backend    │ ──────▶ Redis (holds, live notices)
                                    │  (FastAPI)   │
 Kiosk screen ─▶ machine controller ◀─WebSocket──┘
                 (simulator; a real machine later)
```

A pickup, step by step:
1. The **app** reserves. The **backend** places an atomic hold in **Redis** and records the reservation in **PostgreSQL**.
2. The app pays. The backend *authorises* the mock payment and returns a 6-digit code and a QR code, shown once.
3. At the machine, the customer types the code on the **kiosk**. The kiosk hands it to its **machine controller**, which sends it to the backend over its signed WebSocket.
4. The backend checks the code and tells the machine to dispense. The machine reports the result, and the backend marks the reservation dispensed and *captures* the payment.
5. Every change is announced on the **live channel**: the dashboard and the app update without refreshing.

If the customer doesn't come, the hold expires: the unit is back on sale, the payment is *voided*, and the app and dashboard say so.

---

## 3. The API in Swagger

Open http://localhost:8000/docs.

**3.1 Browse without an account**
1. `GET /api/v1/machines/nearby`. Click **Try it out**, enter `lat` = `44.44`, `lng` = `26.08`, `radius_m` = `20000`, then **Execute**.
   **You should see:** six machines, nearest first, each with `distance_m`, `online: true` and `units_available`. Medicina has `status: "maintenance"`.
2. Copy one machine's `id` into `GET /api/v1/machines/{machine_id}`.
   **You should see:** its slots, each with the product, its price in `price_cents`, and `available`.
3. `GET /api/v1/products` lists the catalogue.

**3.2 Sign in**
1. `POST /api/v1/auth/login` with `{"email": "customer@carepoint.demo", "password": "carepoint-customer-demo"}`.
   **You should see:** `access_token` and `refresh_token`.
2. Click **Authorize** (top right), paste the `access_token` value, then **Authorize** and **Close**.
3. `GET /api/v1/me` shows the customer.

Access tokens last 15 minutes. If calls start returning 401, log in again and re-authorize.

**3.3 Reserve, pay, pick up**
1. `POST /api/v1/reservations` with `{"slot_id": "<a slot_id from 3.1 with available ≥ 1>", "idempotency_key": "test-0001"}`.
   **You should see:** 201 with `status: "held"` and an `expires_at` 15 minutes ahead.
2. Execute the **same request again**.
   **You should see:** 200 and the **same** `id`: a repeated tap doesn't hold a second unit.
3. Re-read that machine (3.1, step 2).
   **You should see:** that slot's `available` is one lower.
4. `POST /api/v1/reservations/{reservation_id}/checkout` with `{"payment_method": "pm_card_ok"}`.
   **You should see:** the reservation `paid`, the order `authorized`, and `pickup.code`, a 6-digit code. **Write it down: it is shown only once.**
5. Open that machine's kiosk (section 5) and type the code.
   **You should see:** the kiosk shows the item coming out, then "Please take your item".
6. `GET /api/v1/orders`.
   **You should see:** that order with `reservation_status: "dispensed"` and `payment_status: "captured"`.

**3.4 Things that should be refused**

| Try | You should see |
|---|---|
| Checkout with `{"payment_method": "pm_card_declined"}` on a new reservation | 402 `payment_declined`; the reservation stays `held` and can be paid again with `pm_card_ok` |
| Checkout with `pm_card_provider_error` | 503 `payment_unavailable`; nothing changes |
| Checkout with a card number such as `{"payment_method": "4111111111111111"}` | 422: only payment tokens are accepted, never card numbers |
| Four held reservations at once | the 4th gets 409 `too_many_active` (the limit is 3 per customer) |
| Reserve more units than are available (`"quantity": 10` on a small slot) | 409 `insufficient_stock` with how many are `available` |
| Reserve at Medicina (maintenance) | 409 `not_sellable` |
| `POST /api/v1/reservations/{id}/pickup-code` on a paid reservation | a **new** code; the old one is now refused at the kiosk |
| `DELETE /api/v1/reservations/{id}` on a held or paid reservation | 204; it becomes `cancelled`, the unit is back, and a paid order is `voided` |
| Any `/api/v1/admin/...` endpoint as the customer | 403 |
| Any reservation endpoint with someone else's reservation id | 404, never their data |

---

## 4. The operator dashboard

Open http://localhost:3000.

1. **A customer can't get in.** Sign in as the customer.
   **You should see:** "No access". Click **Sign in with another account**.
2. **Overview.** Sign in as the operator.
   **You should see:**
   - KPI tiles: reservations, pickup rate, expired holds, hold-to-pickup time, stock-outs, machines online and uptime;
   - a map of Bucharest with six coloured markers;
   - the machine list;
   - a green **Live** indicator at the bottom left.
3. **A machine.** Click a machine in the list.
   **You should see:** temperature, door, firmware and last seen; a grid of slots (low and sold-out slots flagged); and recent events.
4. **Restock.**
   1. Click a slot, choose **Restock (units added)**, enter `2` in **Units** and click **Record**.
   2. **You should see:** "Recorded… The machine was told." The slot's count goes up by 2, and the history shows your email.
   3. Open the same machine's kiosk: its count went up too.
5. **An adjustment.** Choose **Adjustment (counted)** with a negative number.
   **You should see:** it's refused without a **Note**, accepted with one.
6. **Maintenance.** Click **Put in maintenance**, then take it out again.
   **You should see:** the status changes. While it's in maintenance, the app can't reserve there.
7. **Reservations.** Open **Reservations**, then reserve something as the customer (Swagger or the app).
   **You should see:** the row appear **without refreshing**, then change to paid, dispensed or expired as it happens. Filter by status; **Cancel** an active one.
8. **Products.** Add a product, change its price, deactivate it, reactivate it, and delete it.
   **You should see:** each change take effect. Deleting a product that a slot uses is refused: deactivate it instead.

---

## 5. The machine screens (kiosks)

Open http://localhost:8080 and pick a machine, or go straight to http://localhost:8080/kiosk/CP-BUC-0001.

**You should see:** "Online" at the top right, a keypad on the left, and the slots with their counts on the right.

| Try | You should see |
|---|---|
| Type a wrong 6-digit code | "Code not recognised" |
| Type a right code (from Swagger or the app) | the slot highlights, the item comes out, then "Please take your item"; that slot's count drops by one |
| The same code again | refused: codes are single-use |
| A code from **another** machine | refused: a code works only at the machine holding the item |
| **Scan QR code instead**, then paste the app's QR content (`CP1:…`) | dispensed, like the code |
| 20+ wrong codes within a minute | the machine asks you to wait, even for a right code, then recovers (brute-force protection) |

**The operator panel** (⚙, bottom right) switches on the faults used in section 7:
- **Offline**;
- **Jam**;
- **Partial dispense**;
- **Lose connection right after the next dispense**;
- **Seconds per item**;
- **Restock all slots**.

---

## 6. The customer app on your phone

**6.1 Start it**
1. Install **Expo Go** (App Store or Play Store). Your phone and computer must be on the **same Wi-Fi**.
2. Find your computer's address: run `ipconfig` and use the **Wi-Fi** adapter's IPv4 address (for example `192.168.100.18`), not a WSL or VirtualBox one.
3. On the phone's browser, open `http://<that address>:8000/health`.
   **You should see:** `{"status":"ok"}`. If not, allow inbound TCP **8000** and **8081** for **Private** networks in Windows Defender Firewall, and make sure Windows treats the Wi-Fi as Private.
4. In PowerShell:

   ```powershell
   cd mobile; npm ci; $env:EXPO_PUBLIC_API_URL="http://192.168.100.18:8000"; npx expo start
   ```

5. Scan the QR code: with the Camera app on iPhone, or from inside Expo Go on Android. If Expo Go can't load the app, run `npx expo start --tunnel` instead.

**6.2 Try it**

| Do | You should see |
|---|---|
| Sign in as the customer (or **New here? Create an account**; passwords need 12+ characters) | the **Nearby** screen |
| Allow location (or refuse it) | a map with pins and a list sorted by distance. If you refuse, or you're far from Bucharest, there's a banner and the Bucharest demo area |
| Tap **Universitate** | its products, prices, "N left", low-stock and sold-out labels |
| Tap **Reserve** on a product | "Your reservation", "Held for you" and a countdown |
| Tap **Demo: try a declined card** | "Your card was declined"; still payable |
| Tap **Pay** | a QR code and a 6-digit code, with "Pick up within" and the countdown |
| On your computer, type that code on the machine's kiosk | the phone switches to "Picked up" **by itself** |
| Go back to Nearby, then **My orders** | the order under history, "Picked up · Charged" |
| Reserve and pay again, then close Expo Go completely and reopen it | still signed in; the map shows your running hold with its countdown. The code is **not** shown again (it's never saved on the phone): **Show my pickup code** issues a new one |
| **Cancel reservation**, then confirm | "Cancelled. You haven't been charged." |
| **Sign out** (top left) | back to sign-in |

---

## 7. Failure scenarios

Each of these is also covered by automated tests (section 9). Here you watch them happen.

**7.1 A hold expires.**
1. Start demo mode (section 1).
2. Reserve and pay in the app or Swagger, then wait about a minute.
3. **You should see:**
   - the app switches to "This hold ran out… You haven't been charged" with **Reserve again**;
   - the dashboard's Reservations row turns **expired · voided**;
   - the item is available again.

**7.2 A machine goes offline.**
1. On the kiosk's ⚙ panel, tick **Offline**, or stop all machines with `docker compose stop simulator`.
2. **You should see:**
   - within seconds the dashboard marks the machine offline, and the kiosk shows "Offline";
   - in the app, the machine is red, **Reserve** is disabled, and a reservation there warns that the machine is offline but the hold stays and you're only charged on pickup.
3. Untick, or run `docker compose start simulator`. Everything comes back online.

**7.3 A jammed machine.**
1. Set **Jam** → **next dispense**, then type a valid code.
2. **You should see:** the kiosk reports a problem. Nothing came out, so the reservation **stays paid** and the same code can be tried again.
3. The second attempt dispenses, because the jam was for the next dispense only.

**7.4 A partial dispense.**
1. Tick **Partial dispense**.
2. Make a reservation for **2 units**: in Swagger, `"quantity": 2`.
3. Pay and type the code.
4. **You should see:** only some units come out. The reservation is **cancelled and not charged** (an incomplete order is never charged), and the units that did come out leave the stock.

**7.5 The connection drops right after dispensing.**
1. Tick **Lose connection right after the next dispense**, then pick something up.
2. **You should see:**
   - the machine reconnects within seconds and reports the result it kept;
   - the reservation becomes dispensed and charged **exactly once**.

**7.6 The phone loses its connection.**
1. With the app open on a reservation, switch the phone to airplane mode.
2. **You should see:** "Reconnecting… live updates are paused." at the bottom. The countdown keeps running.
3. Switch airplane mode off, and change something in the meantime (for example pick up or cancel from Swagger).
4. **You should see:** the banner goes away and the screen catches up on its own.

**7.7 Redis loses its data.**
1. Run `docker compose exec redis redis-cli FLUSHALL`.
2. **You should see:**
   - the next reservation still works, and existing holds still count: the backend rebuilds each slot's holds from PostgreSQL the first time it is touched;
   - nothing oversells.
3. Also note: the live channels reconnect, the rate-limit counters reset, and you may need to sign in again.

**7.8 Two people want the last unit.** Covered by the load test (`docs/benchmarks.md`): 500 simultaneous customers, exactly one wins every time. By hand: make two customer accounts, find a slot with 1 available, and reserve it from both as fast as you can. One gets the hold; the other gets `insufficient_stock`.

---

## 8. Logs and metrics

- **Logs:**

  ```bash
  docker compose logs -f backend --no-log-prefix
  ```

  Each line is JSON. Every API response carries an `X-Request-ID` header (visible in Swagger's response headers). Find everything that request did with:

  ```bash
  docker compose logs backend --no-log-prefix | grep <id>
  ```

- **Metrics:** `GET http://localhost:8000/metrics` needs an operator's token, so it answers 401 in a plain browser. In Swagger it isn't listed. Use:

  ```bash
  curl -H "Authorization: Bearer <operator access token>" http://localhost:8000/metrics
  ```

  **You should see:** request counts and latency per route, `carepoint_machines{state=…}`, `carepoint_reservations{status=…}` and `carepoint_units_available`.

---

## 9. The automated tests

What CI runs on every change, and how to run it yourself. [`documentation/WORKFLOW.md`](../documentation/WORKFLOW.md) has the details.

| What | Command | Covers |
|---|---|---|
| Backend | `cd backend` then `python -m pytest` (needs `docker compose up -d db redis`) | 500+ tests against real PostgreSQL and Redis: every endpoint, the race tests, the machine protocol, live channels, logs and metrics |
| Simulator | `cd hardware_sim` then `python -m pytest` | the simulated machine's protocol and recovery rules |
| App | `cd mobile` then `npm test` | 86 tests: API client, stores, live connection, screens |
| Kiosk in a browser | `cd e2e` then `node kiosk-smoke.mjs` | wrong code, right code, QR, stock going down |
| Dashboard in a browser | `cd e2e` then `node dashboard-smoke.mjs` | customer refused, restock, live feed, products, offline and back |
| **The whole story** | `cd e2e` then `node story.mjs` (with demo mode on, it includes the expiry) | reserve → pay → kiosk → dispensed and captured; reserve → pay → expired and voided; with the live notices |
| Load test | see [`loadtest/`](../loadtest) and [`docs/benchmarks.md`](benchmarks.md) | 500 customers racing for the last unit |

The e2e scripts need `cd e2e && npm ci && npx playwright install chromium` once.

---

## 10. Reset and troubleshooting

| Problem | Fix |
|---|---|
| Start completely fresh | `docker compose down --volumes`, then `docker compose up --build --wait` |
| Stock is getting low after lots of testing | the kiosk ⚙ panel's **Restock all slots**, or the dashboard's restock form |
| "too_many_active" | the customer already has 3 holds: cancel some (Swagger `GET /api/v1/reservations`, then `DELETE`), or let them expire |
| A code is refused that should work | it works only at the machine it was reserved at, only while paid and unexpired, and only the latest one if you asked for a new code |
| The app says "Can't reach CarePoint" | see 6.1 step 3 (firewall / same Wi-Fi), and that `EXPO_PUBLIC_API_URL` is your Wi-Fi IP. After changing it, run `npx expo start --clear` |
| A machine stays offline | `docker compose logs simulator`; check its ⚙ panel's **Offline** box; `docker compose restart simulator` |
| Everything is slow or odd | `docker compose ps` (all `healthy`?), then `docker compose logs --no-color > logs.txt` |
