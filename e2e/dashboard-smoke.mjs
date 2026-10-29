// Phase 8 "done when": drive the operator dashboard in a real browser against
// the Compose stack. A customer account is refused; an operator sees every
// machine online, records a restock and sees the stock update (on the
// dashboard and on the machine itself), sees a new reservation arrive live, and
// sees machines go offline when the simulator stops - all without reloading.
//
//   DASHBOARD_URL=http://localhost:3000 node dashboard-smoke.mjs
//
// Screenshots of each step land in e2e/artifacts/.

import { execFileSync } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";

const DASHBOARD = process.env.DASHBOARD_URL ?? "http://localhost:3000";
const API = process.env.API_URL ?? "http://localhost:8000/api/v1";
const KIOSK = process.env.KIOSK_URL ?? "http://localhost:8080";
const ARTIFACTS = new URL("./artifacts/", import.meta.url).pathname;
const OPERATOR = { email: "operator@carepoint.demo", password: "carepoint-operator-demo" };
const CUSTOMER = { email: "customer@carepoint.demo", password: "carepoint-customer-demo" };
const LIVE = { timeout: 15_000 }; // a notice, not a reload, must bring the change

function check(condition, message) {
  if (!condition) throw new Error(`FAILED: ${message}`);
}

async function json(url, init = {}) {
  const response = await fetch(url, { ...init, headers: { "Content-Type": "application/json", ...init.headers } });
  if (!response.ok) throw new Error(`${url}: ${response.status} ${await response.text()}`);
  return response.json();
}

async function signIn(page, who) {
  await page.goto(DASHBOARD);
  await page.getByLabel("Email").fill(who.email);
  await page.getByLabel("Password").fill(who.password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

function compose(...args) {
  execFileSync("docker", ["compose", ...args], { stdio: "inherit", cwd: new URL("..", import.meta.url).pathname });
}

await mkdir(ARTIFACTS, { recursive: true });
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));

try {
  // 1. A customer account gets no dashboard.
  await signIn(page, CUSTOMER);
  await page.getByRole("alert").filter({ hasText: "No access" }).waitFor();
  console.log("1. customer account -> \"No access\"");
  await page.screenshot({ path: `${ARTIFACTS}dash-1-no-access.png` });
  await page.getByRole("button", { name: "Sign in with another account" }).click();

  // 2. The operator sees every machine online, with KPIs.
  await signIn(page, OPERATOR);
  await page.getByTestId("live").filter({ hasText: "Live" }).waitFor();
  const rows = page.getByTestId("machines").locator("tbody tr");
  await rows.first().waitFor();
  const total = await rows.count();
  check(total === 6, `expected 6 machines, saw ${total}`);
  await page.getByTestId("machines").getByText("offline", { exact: true }).first().waitFor({ state: "detached" });
  console.log(`2. operator overview: ${total} machines, none offline; live notices connected`);
  await page.screenshot({ path: `${ARTIFACTS}dash-2-overview.png` });

  // 3. Restock a low slot; the dashboard and the machine both count it.
  await page.getByTestId("machine-CP-BUC-0003").click();
  await page.getByTestId("slot-A1").click();
  const before = Number(await page.getByTestId("qty-A1").textContent());
  const machineBefore = (await json(`${KIOSK}/api/machines/CP-BUC-0003`)).slots.find((s) => s.slot === "A1").quantity;
  await page.getByRole("spinbutton", { name: "Units" }).fill("2");
  await page.getByRole("button", { name: "Record" }).click();
  await page.getByText(/Recorded: now \d+\/\d+\. The machine was told\./).waitFor();
  await page.getByTestId("qty-A1").filter({ hasText: String(before + 2) }).waitFor(LIVE);
  await page.getByTestId("history").getByText(OPERATOR.email).first().waitFor();
  let machineAfter = machineBefore;
  for (let i = 0; i < 50 && machineAfter !== machineBefore + 2; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 100));
    machineAfter = (await json(`${KIOSK}/api/machines/CP-BUC-0003`)).slots.find((s) => s.slot === "A1").quantity;
  }
  check(machineAfter === machineBefore + 2, `machine counted ${machineBefore} -> ${machineAfter}`);
  console.log(`3. restock +2 on CP-BUC-0003 A1: dashboard ${before} -> ${before + 2}, machine ${machineBefore} -> ${machineAfter}`);
  await page.screenshot({ path: `${ARTIFACTS}dash-3-restock.png` });

  // 4. A customer reserves in the app; the reservation appears live.
  await page.getByRole("link", { name: "Reservations" }).click();
  await page.getByTestId("reservations").waitFor();
  const { access_token: token } = await json(`${API}/auth/login`, { method: "POST", body: JSON.stringify(CUSTOMER) });
  const machineId = (await json(`${KIOSK}/api/machines/CP-BUC-0003`)).machine_id;
  const slot = (await json(`${API}/machines/${machineId}`)).slots.find((s) => s.slot_code === "A1");
  const reservation = await json(`${API}/reservations`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: JSON.stringify({ slot_id: slot.slot_id }),
  });
  const row = page.getByTestId("reservations").locator("tr", { hasText: CUSTOMER.email }).filter({ hasText: "held" });
  await row.first().waitFor(LIVE);
  console.log("4. customer reservation appeared in the live feed");
  await page.screenshot({ path: `${ARTIFACTS}dash-4-reservations.png` });
  await fetch(`${API}/reservations/${reservation.id}`, { method: "DELETE", headers: { Authorization: `Bearer ${token}` } });

  // 5. Product management: add, deactivate, delete.
  await page.getByRole("link", { name: "Products" }).click();
  const sku = `E2E-${Date.now().toString(36).toUpperCase()}`;
  await page.getByLabel("SKU").fill(sku);
  await page.getByLabel("Product name").fill("E2E lip balm");
  await page.getByLabel("New price").fill("12.50");
  await page.getByRole("button", { name: "Add" }).click();
  const productRow = page.getByTestId("products").locator("tr", { hasText: sku });
  await productRow.getByText("on sale").waitFor();
  await productRow.getByRole("button", { name: "Deactivate" }).click();
  await productRow.getByText("inactive").waitFor();
  page.once("dialog", (dialog) => void dialog.accept());
  await productRow.getByRole("button", { name: "Delete" }).click();
  await productRow.waitFor({ state: "detached" });
  console.log(`5. product ${sku} added, deactivated and deleted`);

  // 6. Stop the simulator: machines go offline on the open overview.
  await page.getByRole("link", { name: "Overview" }).click();
  await page.getByTestId("machine-CP-BUC-0001").getByText("online").waitFor();
  compose("stop", "simulator");
  await page.getByTestId("machine-CP-BUC-0001").getByText("offline").waitFor(LIVE);
  // Every machine leaves "online" (CP-BUC-0006 is seeded in maintenance, shown as such).
  await page.getByTestId("machines").getByText("online", { exact: true }).first().waitFor({ state: "detached", ...LIVE });
  const offline = await page.getByTestId("machines").getByText("offline", { exact: true }).count();
  console.log(`6. simulator stopped -> no machine online, ${offline} shown offline, without a reload`);
  await page.screenshot({ path: `${ARTIFACTS}dash-6-offline.png` });
  check(offline === 5, `expected the 5 active machines offline, saw ${offline}`);

  compose("start", "simulator");
  await page.getByTestId("machine-CP-BUC-0001").getByText("online").waitFor({ timeout: 60_000 });
  console.log("7. simulator started again -> back online");
  check(errors.length === 0, `page errors: ${errors.join("; ")}`);
  console.log("OK");
} finally {
  await browser.close();
}
