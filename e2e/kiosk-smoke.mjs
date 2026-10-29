// Phase 7 "done when": drive the kiosk of a simulated machine in a real
// browser. A customer pays through the API; at the kiosk a wrong code is
// refused, the right one is typed on the keypad, the item is dispensed, and
// stock goes down on the machine's screen and in the app's API.
//
//   API_URL=http://localhost:8000/api/v1 KIOSK_URL=http://localhost:8080 node kiosk-smoke.mjs
//
// Screenshots of each step land in e2e/artifacts/.

import { fileURLToPath } from "node:url";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";

const API = process.env.API_URL ?? "http://localhost:8000/api/v1";
const KIOSK = process.env.KIOSK_URL ?? "http://localhost:8080";
const SERIAL = "CP-BUC-0001";
const ARTIFACTS = fileURLToPath(new URL("./artifacts/", import.meta.url));

async function api(method, path, body, token) {
  const response = await fetch(API + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${method} ${path}: ${response.status} ${await response.text()}`);
  return response.status === 204 ? null : response.json();
}

function check(condition, message) {
  if (!condition) throw new Error(`FAILED: ${message}`);
}

async function availableA1(machineId) {
  const machine = await api("GET", `/machines/${machineId}`);
  return machine.slots.find((slot) => slot.slot_code === "A1");
}

async function typeOnKeypad(page, digits) {
  for (const digit of digits) {
    await page.getByRole("group", { name: "Keypad" }).getByRole("button", { name: digit, exact: true }).click();
  }
}

await mkdir(ARTIFACTS, { recursive: true });
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
page.on("pageerror", (error) => {
  throw new Error(`kiosk page error: ${error.message}`);
});

try {
  const { access_token: token } = await api("POST", "/auth/login", {
    email: "customer@carepoint.demo",
    password: "carepoint-customer-demo",
  });

  await page.goto(`${KIOSK}/kiosk/${SERIAL}`);
  await page.getByTestId("status").filter({ hasText: "Online" }).waitFor({ timeout: 30_000 });
  const before = Number(await page.getByTestId("qty-A1").textContent());
  const machineId = (await (await fetch(`${KIOSK}/api/machines/${SERIAL}`)).json()).machine_id;
  const slot = await availableA1(machineId);
  console.log(`1. kiosk online; A1 on the machine: ${before}, available in the app: ${slot.available}`);
  await page.screenshot({ path: `${ARTIFACTS}1-idle.png` });

  const reservation = await api("POST", "/reservations", { slot_id: slot.slot_id }, token);
  const paid = await api("POST", `/reservations/${reservation.id}/checkout`, { payment_method: "pm_card_ok" }, token);
  const code = paid.pickup.code;
  console.log(`2. reserved and paid in the app; pickup code ${code}`);

  await typeOnKeypad(page, code === "000000" ? "111111" : "000000");
  await page.getByTestId("notice").filter({ hasText: "Code not recognised" }).waitFor();
  console.log("3. wrong code on the keypad -> refused on screen");
  await page.screenshot({ path: `${ARTIFACTS}2-wrong-code.png` });

  await typeOnKeypad(page, code);
  await page.getByText(/^Dispensing from A1/).waitFor({ timeout: 10_000 });
  await page.screenshot({ path: `${ARTIFACTS}3-dispensing.png` });
  await page.getByTestId("done").waitFor({ timeout: 30_000 });
  await page.screenshot({ path: `${ARTIFACTS}4-done.png` });
  const after = Number(await page.getByTestId("qty-A1").textContent());
  console.log(`4. right code -> "Please take your item"; A1 on the machine: ${before} -> ${after}`);
  check(after === before - 1, `machine stock ${before} -> ${after}, expected -1`);

  let order;
  for (let i = 0; i < 100; i += 1) {
    order = (await api("GET", "/orders", undefined, token)).find((o) => o.reservation_id === reservation.id);
    if (order.reservation_status === "dispensed" && order.payment_status === "captured") break;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  const appAfter = (await availableA1(machineId)).available;
  console.log(`5. order ${order.reservation_status} / ${order.payment_status}; A1 available in the app: ${appAfter}`);
  check(order.reservation_status === "dispensed", "order not dispensed");
  check(order.payment_status === "captured", "payment not captured");
  check(appAfter === slot.available - 1, `app availability ${slot.available} -> ${appAfter}, expected -1`);

  // The QR route: what the app's QR code contains, pasted into the scanner
  // dialog (headless Chromium has no camera, like a desktop demo).
  await page.getByTestId("done").waitFor({ state: "detached", timeout: 15_000 });
  const second = await api("POST", "/reservations", { slot_id: slot.slot_id }, token);
  const qr = (await api("POST", `/reservations/${second.id}/checkout`, { payment_method: "pm_card_ok" }, token))
    .pickup.qr_payload;
  await page.getByRole("button", { name: "Scan QR code instead" }).click();
  await page.getByLabel("QR content").fill(qr);
  await page.screenshot({ path: `${ARTIFACTS}5-qr.png` });
  await page.getByRole("button", { name: "Use", exact: true }).click();
  await page.getByTestId("done").waitFor({ timeout: 30_000 });
  const afterQr = Number(await page.getByTestId("qty-A1").textContent());
  console.log(`6. QR payload ${qr.slice(0, 12)}… -> dispensed; A1 on the machine: ${after} -> ${afterQr}`);
  check(afterQr === after - 1, `machine stock ${after} -> ${afterQr} after the QR pickup, expected -1`);
  console.log("OK");
} finally {
  await browser.close();
}
