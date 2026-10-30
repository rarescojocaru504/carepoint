# Pitch kit

Everything needed to present CarePoint.

| File | What it is |
|---|---|
| [`one-pager.md`](one-pager.md) | The problem, the solution, how it works, the technology, the evidence, and what's next. Hand it out or put it on a slide. |
| [`architecture.svg`](architecture.svg) | The architecture diagram (also used in the README) |
| [`demo-video.webm`](demo-video.webm) | **A one-minute recording (65 s) of the whole story**, as a backup if the live demo fails: reserve, pay, pick up at the machine, then a hold that expires and is voided. Opens in any browser (drag it into a tab) or VLC. |
| [`screenshots/`](screenshots) | The video's key moments as images, for slides |
| [`hardware-prototype.md`](hardware-prototype.md) | Parts list and cost estimate for one physical machine, mapped onto the machine protocol |

The video and screenshots come from `e2e/story.mjs` (ADR-030) on a fresh stack. Every CI run records a new copy, available as the `story-video` artifact.

---

## Running the live demo

**The day before**
1. On the presentation laptop:
   ```bash
   docker compose up --build --wait
   ```
2. Sign in to the dashboard (http://localhost:3000) as the operator, and open a kiosk (http://localhost:8080/kiosk/CP-BUC-0001).
3. Start the app on your phone in Expo Go, using the laptop's Wi-Fi address ([manual testing guide](../manual-testing-guide.md), section 6). Check that the phone shows the six machines.
4. Watch the backup video once, so you can narrate over it if you need to.

**Just before you start**
1. Switch to short holds, so an expiry happens within a minute:
   ```bash
   RESERVATION_HOLD_SECONDS=60 docker compose up -d --wait backend
   ```
   In PowerShell:
   ```powershell
   $env:RESERVATION_HOLD_SECONDS=60; docker compose up -d --wait backend; Remove-Item Env:RESERVATION_HOLD_SECONDS
   ```
2. Make the system look alive:
   ```bash
   docker compose exec backend python -m app.scripts.demo
   ```
   Simulated customers now reserve, pick up, cancel and let holds expire; the dashboard's feed, map and KPIs keep moving.

**A five-minute script**

| Minute | Show | Say |
|---|---|---|
| 0–1 | The dashboard **Overview** | "Six machines on campus, live. The KPI tiles show reservations, pickup rate and uptime." |
| 1–2 | The phone: map, then **Universitate** | "From my room I can see exactly what's in stock." Reserve something. "It's now held for me; nobody else can take it." |
| 2–3 | **Pay** on the phone; the dashboard's **Reservations** | "Paid: authorised, not charged yet. The operator sees it instantly." |
| 3–4 | Type the code on the kiosk | "At the machine I type my code. Only now am I charged." The phone switches to *Picked up* on its own. |
| 4–5 | A simulated customer's hold turning **expired · voided** in the feed; then the kiosk's ⚙ **Offline** box | "If I don't come, the hold ends and I'm not charged." Tick Offline: "and if a machine goes down, everyone sees it immediately." Untick it. |

**If something goes wrong:** play `demo-video.webm`. It shows the same story.

**Afterwards:** stop the demo script with Ctrl+C, and return to 15-minute holds:
```bash
docker compose up -d --wait backend
```
