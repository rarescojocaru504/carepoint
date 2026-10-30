// The MVP user story, end to end (CLAUDE.md, Phase 11):
//
//   A user sees nearby machines with live stock, reserves an item (held for
//   15 minutes) and pays. At the machine they enter the one-time code and the
//   machine dispenses it. If they don't arrive in time, the hold expires, the
//   stock returns and the payment is voided.
//
// The customer's side uses exactly the API calls the mobile app makes
// (mobile/src/store/*), including its live notice stream; the machine's side is
// the real kiosk screen in a browser, talking to a simulated machine over the
// real protocol. Part 2 (expiry) runs when the server's holds are short:
//
//   RESERVATION_HOLD_SECONDS=40 docker compose up -d --wait backend
//   node story.mjs                    # assertions only
//   STORY_VIDEO=1 node story.mjs      # also records e2e/artifacts/story-video/*.webm
//
// The video moves between the operator dashboard and the kiosk, with captions
// for what the customer does in the app (the app itself is native, not a page).

import { mkdir, readdir, rename, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const API = process.env.API_URL ?? "http://localhost:8000/api/v1";
const KIOSK = process.env.KIOSK_URL ?? "http://localhost:8080";
const DASHBOARD = process.env.DASHBOARD_URL ?? "http://localhost:3000";
const VIDEO = process.env.STORY_VIDEO === "1";
const ARTIFACTS = fileURLToPath(new URL("./artifacts/", import.meta.url));
const VIDEO_DIR = `${ARTIFACTS}story-video/`;
const CUSTOMER = { email: "customer@carepoint.demo", password: "carepoint-customer-demo" };
const OPERATOR = { email: "operator@carepoint.demo", password: "carepoint-operator-demo" };
const MAX_SHORT_HOLD_S = 90;
// Video pacing: long enough to read a caption, zero when only testing.
const PAUSE_MS = VIDEO ? 2500 : 0;

function check(condition, message) {
  if (!condition) throw new Error(`FAILED: ${message}`);
}

const pause = (ms = PAUSE_MS) => new Promise((resolve) => setTimeout(resolve, ms));

async function eventually(probe, what, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await probe();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`FAILED: timed out waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

// --- the app's API calls ------------------------------------------------------------

async function api(method, path, { body, token, ok = true } = {}) {
  const response = await fetch(API + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (ok && !response.ok) {
    throw new Error(`${method} ${path}: ${response.status} ${await response.text()}`);
  }
  const data = response.status === 204 ? null : await response.json();
  return { status: response.status, data };
}

/** The app's live connection: GET /api/v1/events, read as a stream. */
function listen(token) {
  const notices = [];
  const controller = new AbortController();
  const done = (async () => {
    const response = await fetch(`${API}/events`, {
      headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
      signal: controller.signal,
    });
    check(response.ok, `event stream answered ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done: ended, value } = await reader.read();
      if (ended) return;
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n\n")) !== -1) {
        const event = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        for (const line of event.split("\n")) {
          if (line.startsWith("data: ")) notices.push(JSON.parse(line.slice(6)));
        }
      }
    }
  })().catch((error) => {
    if (!controller.signal.aborted) throw error;
  });
  return {
    notices,
    waitFor: (predicate, what, timeoutMs) =>
      eventually(() => notices.find(predicate), `notice: ${what}`, timeoutMs),
    close: async () => {
      controller.abort();
      await done;
    },
  };
}

const newIdempotencyKey = () => `story-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

// --- the browser: kiosk and dashboard -----------------------------------------------

let captions = 0;
async function caption(page, text) {
  if (!VIDEO) return;
  await page.evaluate((content) => {
    let box = document.getElementById("story-caption");
    if (!box) {
      box = document.createElement("div");
      box.id = "story-caption";
      Object.assign(box.style, {
        position: "fixed", left: "24px", right: "24px", bottom: "24px", zIndex: "2147483647",
        padding: "16px 20px", borderRadius: "12px", background: "rgba(15, 23, 42, 0.92)",
        color: "#fff", font: "600 22px/1.35 system-ui, sans-serif", pointerEvents: "none",
        boxShadow: "0 8px 30px rgba(0,0,0,.3)",
      });
      document.body.appendChild(box);
    }
    box.textContent = content;
  }, text);
  captions += 1;
  await page.screenshot({ path: `${VIDEO_DIR}step-${captions}.png` });
}

async function typeOnKeypad(page, digits) {
  for (const digit of digits) {
    await page.getByRole("group", { name: "Keypad" }).getByRole("button", { name: digit, exact: true }).click();
    if (VIDEO) await pause(250);
  }
}

async function openReservationsFeed(page) {
  await page.goto(DASHBOARD);
  const email = page.getByLabel("Email");
  const live = page.getByTestId("live").filter({ hasText: "Live" });
  // Signed in already (the dashboard keeps its session per tab), or not: wait
  // for whichever screen renders rather than guessing before React has drawn.
  await email.or(live).first().waitFor({ timeout: 30_000 });
  if (await email.isVisible()) {
    await email.fill(OPERATOR.email);
    await page.getByLabel("Password").fill(OPERATOR.password);
    await page.getByRole("button", { name: "Sign in" }).click();
  }
  await live.waitFor({ timeout: 30_000 });
  await page.getByRole("link", { name: "Reservations" }).click();
  await page.getByTestId("reservations").waitFor();
}

function feedRow(page, reservation, status) {
  return page
    .getByTestId("reservations")
    .locator("tr", { hasText: CUSTOMER.email })
    .filter({ hasText: reservation.product_name })
    .filter({ hasText: status })
    .first();
}

// --- the story ----------------------------------------------------------------------

await mkdir(ARTIFACTS, { recursive: true });
if (VIDEO) await rm(VIDEO_DIR, { recursive: true, force: true });
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1280, height: 720 },
  ...(VIDEO ? { recordVideo: { dir: VIDEO_DIR, size: { width: 1280, height: 720 } } } : {}),
});
const page = await context.newPage();
page.on("pageerror", (error) => {
  throw new Error(`page error: ${error.message}`);
});
let stream;

try {
  // 1. Open the app: sign in and look at the map.
  const { data: tokens } = await api("POST", "/auth/login", { body: CUSTOMER });
  const token = tokens.access_token;
  const { data: me } = await api("GET", "/me", { token });
  stream = listen(token);
  await stream.waitFor((n) => n.topic === "hello", "hello", 10_000);

  // An online, fault-free machine with a slot of >= 2 available units. Waited
  // for: right after the backend restarts (as CI does for short holds) every
  // machine is reconnecting, with backoff, and briefly reads as offline.
  const { nearby, machine, slot } = await eventually(async () => {
    const { data: machines } = await api("GET", "/machines/nearby?lat=44.44&lng=26.08&radius_m=20000&limit=50");
    const kiosks = await (await fetch(`${KIOSK}/api/machines`)).json();
    const simOf = new Map(kiosks.map((k) => [k.machine_id, k]));
    for (const candidate of machines) {
      const sim = simOf.get(candidate.id);
      if (candidate.status !== "active" || !candidate.online || !sim || sim.faults.offline) continue;
      if (sim.faults.jam !== "off" || sim.faults.partial_next || sim.faults.drop_next_result) continue;
      const { data: detail } = await api("GET", `/machines/${candidate.id}`);
      const usable = detail.slots.find((s) => s.available >= 2);
      if (usable) return { nearby: machines, machine: { ...candidate, serial: sim.serial }, slot: usable };
    }
    return null;
  }, "an online machine with a slot holding at least 2 available units", 90_000);
  console.log(`1. ${me.email} signed in; ${nearby.length} machines nearby; ` +
    `${machine.name} (${machine.serial}) has ${slot.available} × ${slot.product.name}`);

  await openReservationsFeed(page);
  await caption(page, `In the app: ${me.full_name} sees ${nearby.length} machines nearby, with live stock. ` +
    `${machine.name} has ${slot.available} × ${slot.product.name}.`);
  await pause();

  // 2. Reserve: an atomic 15-minute hold (the app sends an idempotency key per tap).
  const key = newIdempotencyKey();
  const reserved = await api("POST", "/reservations", { token, body: { slot_id: slot.slot_id, quantity: 1, idempotency_key: key } });
  check(reserved.status === 201 && reserved.data.status === "held", "reservation not created as held");
  const reservation = reserved.data;
  const retried = await api("POST", "/reservations", { token, body: { slot_id: slot.slot_id, quantity: 1, idempotency_key: key } });
  check(retried.status === 200 && retried.data.id === reservation.id, "a retried tap made a second hold");
  const holdSeconds = Math.round((Date.parse(reservation.expires_at) - Date.parse(reservation.created_at)) / 1000);
  await stream.waitFor((n) => n.topic === "reservation" && n.reservation_id === reservation.id && n.change === "created", "created");
  await stream.waitFor((n) => n.topic === "stock" && n.machine_id === machine.id, "availability at the machine");
  const held = (await api("GET", `/machines/${machine.id}`)).data.slots.find((s) => s.slot_id === slot.slot_id);
  check(held.available === slot.available - 1, `available ${slot.available} -> ${held.available}, expected -1`);
  console.log(`2. reserved (hold ${holdSeconds} s; a retried tap returned the same hold); ` +
    `available ${slot.available} -> ${held.available}; live notices: created, availability`);

  const holdText = holdSeconds >= 60 ? `${Math.round(holdSeconds / 60)} min` : `${holdSeconds} s`;
  // The row first, then the caption: the step's screenshot shows what the caption says.
  await feedRow(page, reservation, "held").waitFor({ timeout: 15_000 });
  await caption(page, `In the app: reserve. The item is held for ${holdText}; ` +
    `nobody else can take it. The operator sees it live.`);
  await pause();

  // 3. Pay (mock card): authorised now, captured only at pickup.
  const { data: paid } = await api("POST", `/reservations/${reservation.id}/checkout`, { token, body: { payment_method: "pm_card_ok" } });
  const code = paid.pickup.code;
  check(paid.reservation.status === "paid" && paid.order.payment_status === "authorized", "checkout did not authorise");
  check(/^\d{6}$/.test(code) && paid.pickup.qr_payload === `CP1:${reservation.id}:${code}`, "bad pickup credential");
  await stream.waitFor((n) => n.reservation_id === reservation.id && n.change === "paid", "paid");
  console.log(`3. paid: payment authorised; pickup code ${code}; live notice: paid`);
  await feedRow(page, reservation, "paid").waitFor({ timeout: 15_000 });
  await caption(page, `In the app: pay ${(reservation.total_cents / 100).toFixed(2)} ${reservation.currency} (authorised, not yet charged). ` +
    `Pickup code ${code.slice(0, 3)} ${code.slice(3)}.`);
  await pause();

  // 4. At the machine: type the code; the machine dispenses; payment captured.
  await page.goto(`${KIOSK}/kiosk/${machine.serial}`);
  await page.getByTestId("status").filter({ hasText: "Online" }).waitFor({ timeout: 60_000 });
  await caption(page, `At ${machine.name}: type the code on the machine's screen (or scan the QR).`);
  await pause(PAUSE_MS / 2);
  const before = Number(await page.getByTestId(`qty-${slot.slot_code}`).textContent());
  await typeOnKeypad(page, code);
  await page.getByTestId("done").waitFor({ timeout: 30_000 });
  await caption(page, "The machine dispenses the item. Only now is the payment captured.");
  const after = Number(await page.getByTestId(`qty-${slot.slot_code}`).textContent());
  check(after === before - 1, `machine stock ${before} -> ${after}, expected -1`);
  await stream.waitFor((n) => n.reservation_id === reservation.id && n.change === "dispensing", "dispensing");
  await stream.waitFor((n) => n.reservation_id === reservation.id && n.change === "dispensed", "dispensed");
  const { data: done } = await api("GET", `/reservations/${reservation.id}`, { token });
  const order = await eventually(async () => {
    const { data: orders } = await api("GET", "/orders", { token });
    const found = orders.find((o) => o.reservation_id === reservation.id);
    return found?.payment_status === "captured" ? found : null;
  }, "payment captured");
  check(done.status === "dispensed", `reservation ${done.status}, expected dispensed`);
  console.log(`4. code typed at the kiosk -> dispensed; machine ${slot.slot_code}: ${before} -> ${after}; ` +
    `order ${order.reservation_status} / ${order.payment_status}; live notices: dispensing, dispensed`);
  await pause();

  // 5. If they don't arrive in time: the hold expires, stock returns, payment voided.
  if (holdSeconds > MAX_SHORT_HOLD_S) {
    console.log(`5. expiry: skipped (holds are ${holdSeconds} s; run with RESERVATION_HOLD_SECONDS<=${MAX_SHORT_HOLD_S})`);
  } else {
    const { data: second } = await api("POST", "/reservations", { token, body: { slot_id: slot.slot_id, quantity: 1, idempotency_key: newIdempotencyKey() } });
    await api("POST", `/reservations/${second.id}/checkout`, { token, body: { payment_method: "pm_card_ok" } });
    const whileHeld = (await api("GET", `/machines/${machine.id}`)).data.slots.find((s) => s.slot_id === slot.slot_id).available;
    await openReservationsFeed(page);
    await feedRow(page, second, "paid").waitFor({ timeout: 15_000 });
    await caption(page, `Another reservation, paid... but the customer doesn't make it. ` +
      `In this demo holds last ${holdSeconds} s instead of 15 min.`);
    await stream.waitFor(
      (n) => n.reservation_id === second.id && n.change === "expired",
      "expired",
      (holdSeconds + 45) * 1000,
    );
    await feedRow(page, second, "expired").waitFor({ timeout: 15_000 });
    await caption(page, "The hold expired: the item went back on sale and the payment was voided. Nobody was charged.");
    const voided = await eventually(async () => {
      const { data: orders } = await api("GET", "/orders", { token });
      const found = orders.find((o) => o.reservation_id === second.id);
      return found?.payment_status === "voided" ? found : null;
    }, "payment voided");
    const back = (await api("GET", `/machines/${machine.id}`)).data.slots.find((s) => s.slot_id === slot.slot_id).available;
    check(back === whileHeld + 1, `available ${whileHeld} -> ${back} after expiry, expected +1`);
    console.log(`5. a second hold ran out -> expired; order ${voided.reservation_status} / ${voided.payment_status}; ` +
      `available ${whileHeld} -> ${back}; live notice: expired`);
    await pause();
  }

  // Privacy (ADR-026): this customer never heard about anyone else's reservation.
  const foreign = stream.notices.filter((n) => n.topic === "reservation" && n.reservation_id === null);
  check(foreign.length === 0, "a reservation notice without a reservation id reached the customer");
  console.log(`6. ${stream.notices.length} live notices received, all of them this customer's own or anonymous`);
  console.log("OK");
} finally {
  await stream?.close();
  await context.close(); // finishes writing the video
  await browser.close();
}

if (VIDEO) {
  const [file] = (await readdir(VIDEO_DIR)).filter((name) => name.endsWith(".webm"));
  await rename(`${VIDEO_DIR}${file}`, `${VIDEO_DIR}carepoint-story.webm`);
  console.log(`video: ${VIDEO_DIR}carepoint-story.webm`);
}
