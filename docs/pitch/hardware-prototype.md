# A physical prototype machine: parts and cost estimate

CarePoint's software is finished, including a [versioned machine protocol](../machine-protocol.md) that a real machine can implement without any change to the backend. This document describes **one prototype vending machine**: what it's made of, what each part does in the protocol, and roughly what it costs. Building it is what the funding would pay for.

> **About the prices.** These are **estimates of retail prices in Romania/EU in late 2026, VAT included**, rounded, for a single unit bought from common electronics retailers. Prices vary by supplier and over time. Treat the totals as a budget range and get quotes before ordering. An exchange rate of **1 EUR ≈ 5 RON** is assumed.

---

## 1. What the prototype must do

It has to behave exactly like one of the six simulated machines:
- **8 slots** (A1–A4, B1–B4), each holding up to about 10 small items: pads, condoms, plasters, a pregnancy test, a cable, and so on;
- a **touchscreen** with a keypad for the 6-digit code;
- a **camera** for QR codes;
- a **pickup hatch** that stays locked until an item has dropped;
- a **service door** with a lock;
- reports of its **door state** and **inside temperature**;
- an always-on **internet connection** to the backend.

## 2. The design in one picture

```
              ┌──────────────────────── enclosure ────────────────────────┐
  Wi-Fi /     │  Raspberry Pi 5 ── touchscreen (keypad, slot display)      │
  4G ◀──────▶ │      │           ── camera (QR codes)                      │
  backend     │      │ GPIO / I²C                                          │
  (WebSocket) │      ├── 8 × MOSFET channels ──▶ 8 × spiral motors (24 V)  │
              │      ├── drop sensor (IR beam across the drop zone)        │
              │      ├── hatch solenoid lock (12 V) + hatch switch         │
              │      ├── service-door reed switch                          │
              │      ├── temperature sensor                                │
              │      └── UPS HAT (rides through power cuts, safe shutdown) │
              │  24 V PSU ──▶ motors;  5 V buck converter ──▶ Pi           │
              └────────────────────────────────────────────────────────────┘
```

**The controller is a Raspberry Pi**, rather than a bare ESP32, because the protocol wants two things that are simpler on a small Linux computer:
- **Ed25519 signing** of every connection, with the same `cryptography` library the simulator already uses;
- **durable storage** for the dispensing rules: "store every result until it is acknowledged" (protocol §6).

The Pi can also run the existing kiosk screen (React) unchanged, and much of the simulator's Python code (`hardware_sim/carepoint_sim/`): the connection, signing, state store and reconnection logic carry over directly, and only the "dispense" step changes from a timer to real motors. An ESP32 is a good later choice for cost, as an I/O co-processor or as the whole controller (§5).

## 3. Parts list and cost estimate

| # | Part | What it's for | Protocol link | Qty | Unit (€) | Total (€) |
|---|---|---|---|---|---|---|
| **Controller and screen** | | | | | | |
| 1 | Raspberry Pi 5, 4 GB | runs the machine software and the kiosk screen | handshake signing (§1), `heartbeat`, all messages | 1 | 60–70 | 60–70 |
| 2 | 27 W USB-C power supply (official) | powers the Pi on the bench | | 1 | 12–15 | 12–15 |
| 3 | 64 GB high-endurance microSD (or NVMe SSD + HAT) | OS and durable state (results kept until acknowledged) | §6 rules 1–3 | 1 | 12–45 | 12–45 |
| 4 | 7″ touchscreen (Raspberry Pi Touch Display 2) | keypad for the code, slot display, messages | `verify_code` (typed) | 1 | 60–70 | 60–70 |
| 5 | Camera Module 3 (or a USB 2D barcode scanner module) | reads the app's QR code | `verify_code` (scanned `CP1:` payload) | 1 | 25–40 | 25–40 |
| **Dispensing** | | | | | | |
| 6 | Spiral vending motor, 24 V DC, with position switch | turns one spiral one revolution per item | `dispense` → `dispense_result` | 8 | 12–20 | 96–160 |
| 7 | Spiral coil (product pusher), sized to the items | holds and pushes items | slot capacity in the planogram (`welcome`) | 8 | 5–10 | 40–80 |
| 8 | 8-channel MOSFET (or relay) driver board | switches the motors from the Pi's GPIO | | 1 | 10–20 | 10–20 |
| 9 | Drop sensor: an IR light curtain or break-beam pairs across the drop zone | confirms each item actually fell; detects jams | honest `units_dispensed`; `error_event` `jam` (§6 rule 4) | 1 | 10–40 | 10–40 |
| **Locks and sensors** | | | | | | |
| 10 | 12 V solenoid lock for the pickup hatch, plus a hatch microswitch | hatch opens only after a dispense | `dispense` flow | 1 | 10–18 | 10–18 |
| 11 | Cam lock for the service door | operator access for restocking | | 1 | 5–10 | 5–10 |
| 12 | Magnetic reed switch on the service door | knows when the door is open | `telemetry` `door`; `error_event` `door_forced` | 1 | 2–5 | 2–5 |
| 13 | Temperature sensor (DS18B20 or SHT31) | inside temperature | `telemetry` `temperature_c`; `temperature_out_of_range` | 1 | 3–10 | 3–10 |
| **Power** | | | | | | |
| 14 | 24 V, ~150 W power supply (enclosed) | motors and solenoid (via a 12 V step-down) | | 1 | 25–40 | 25–40 |
| 15 | 24 V → 5 V 5 A buck converter, and 24 V → 12 V for the lock | powers the Pi and the lock from one supply | | 2 | 6–12 | 12–24 |
| 16 | UPS HAT with batteries for the Pi | finishes the current step and shuts down cleanly on a power cut | `error_event` `power_loss`; §6 rule 1 (never repeat an interrupted dispense) | 1 | 30–45 | 30–45 |
| **Connectivity** | | | | | | |
| 17 | 4G USB modem + data SIM (optional; Wi-Fi is built in) | works where there's no reliable Wi-Fi | the WebSocket connection (§1, §5) | 1 | 35–60 | 0–60 |
| **Enclosure and assembly** | | | | | | |
| 18 | Cabinet: 18 mm plywood or MDF with a polycarbonate front window, hinges, feet | the body of the machine | | 1 | 150–250 | 150–250 |
| 19 | Wiring, connectors, terminal blocks, fuses, cable glands, mounting hardware | assembly | | 1 | 30–50 | 30–50 |
| 20 | 3D-printed parts (motor mounts, drop chute, screen bezel) | assembly | | 1 | 20–40 | 20–40 |
| | **Total** | | | | | **≈ 612–1,092 €** (about **3,060–5,460 RON**) |

**The cost drivers** are the 8 motors and spirals (about €136–240) and the cabinet (about €150–250). The electronics brain (Pi, its power supply, storage, screen, camera and UPS, rows 1–5 and 16) comes to about €199–285.

**Not included:**
- **Labour:** design, assembly, wiring, testing. The software already exists, so this is mostly mechanical and electrical work.
- **Tools:** soldering station, multimeter, drill, access to a 3D printer.
- **The products** to stock it.
- **Certification:** CE marking and electrical safety for public placement, which a pilot in a supervised university space would need to plan for with the faculty.

## 4. A cheaper and sturdier alternative for the mechanics

Second-hand **spiral vending machines** (the common snack type) are often sold cheaply when cafés or offices replace them, typically for a few hundred euros up to about €1,000 depending on size and condition. Such a machine already has the cabinet, the spirals, the motors and a lockable door.

Replacing **only its controller** with parts 1–5, 8–10, 12–13 and 16 from the table, about **€234–378**, gives a robust, public-ready prototype. The mechanics are proven, and only the brain is new.

For a pitch, a small purpose-built tabletop unit (§3) is easier to carry and show. For a pilot, a refurbished machine is likely the more practical choice.

## 5. Mapping the machine onto the protocol

| Protocol step | What the hardware does |
|---|---|
| **Connect** (§1) | The Pi signs the handshake with its own Ed25519 private key, stored on the device and never sent anywhere. The server knows only the public key: `python -m app.scripts.provision_machine` registers it. |
| **`welcome`** | It receives its planogram (slot → product, capacity) and shows it on the screen. |
| **`heartbeat`** every 30 s | Sent by the Pi's software. Uptime comes from the OS. |
| **`telemetry`** | Reed switch → `door`; temperature sensor → `temperature_c`; sent on change and at least every 30 s. |
| **`stock_report`** | The Pi keeps the count per slot: decremented by the drop sensor on each dispense, and set by the operator's restock on the screen or by `adjust_stock`. Sent after connecting and after any change other than a dispense. |
| **`verify_code`** | The customer types the code on the touchscreen, or the camera reads the QR code. |
| **`dispense`** | The command id is written to storage first, then the hatch stays locked while the slot's motor turns. Each item crossing the IR beam is counted; then the hatch unlocks. |
| **`dispense_result`** | `dispensed` with the counted units, or `failed` with the true count and `error_code`: `jam` if the beam saw nothing within the timeout, `motor_fault` if the position switch never triggered. Stored until `ack`ed. |
| **Power cut mid-dispense** | The UPS keeps the Pi up long enough to record what came out. It never restarts the dispense (§6 rule 1), and on reconnect it sends the stored result before the stock report (§6 rule 3). |
| **`error_event`** | Jam, motor fault, sensor fault, forced door (reed switch opens without a service session), temperature out of range, power loss. |

Firmware plan:
1. Start from `hardware_sim/carepoint_sim/` (connection, signing, durable state, reconnection with backoff, the dispensing rules).
2. Replace the simulated dispense with GPIO control of the motors, drop sensor and lock.
3. Run the existing kiosk screen in a full-screen browser on the touchscreen.

The backend, the app and the dashboard need **no changes**: a real machine is just another machine on the same protocol.

## 6. Build plan (indicative)

| Step | Work | Rough time |
|---|---|---|
| 1 | Bench prototype: Pi, screen, camera, one motor, drop sensor and lock on a board; running the protocol against the backend | 1–2 weeks |
| 2 | Enclosure and the full 8-slot mechanism | 2–3 weeks |
| 3 | Reliability: jams, power cuts, network loss; soak test with the demo script (`python -m app.scripts.demo`) driving real pickups | 1–2 weeks |
| 4 | Pilot placement and first restock | 1 week |
