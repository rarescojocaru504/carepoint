import { useMachines } from "../../store/machines";
import { useReservations } from "../../store/reservations";
import { installFakeServer, json } from "../../test-utils/fakeServer";
import { detail, MACHINE_ID, summary } from "../../test-utils/fixtures";
import { reservation, RESERVATION_ID } from "../../test-utils/reservationFixtures";
import type { Notice } from "../live";
import { applyNotice, cancelPendingSync, DEBOUNCE_MS } from "../liveSync";
import { setAccessToken } from "../tokens";

const OTHER_MACHINE = "22222222-2222-4222-8222-222222222299";
const AT = "2026-10-29T12:00:00Z";

function notice(overrides: Partial<Notice>): Notice {
  return {
    topic: "stock",
    machine_id: null,
    reservation_id: null,
    change: "x",
    at: AT,
    ...overrides,
  };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, DEBOUNCE_MS + 50));

function serve() {
  return installFakeServer({
    "GET /api/v1/machines/nearby": () => json(200, [summary()]),
    [`GET /api/v1/machines/${MACHINE_ID}`]: () => json(200, detail({ online: false })),
    [`GET /api/v1/reservations/${RESERVATION_ID}`]: () =>
      json(200, reservation({ status: "expired" })),
    "GET /api/v1/reservations": () => json(200, []),
    "GET /api/v1/orders": () => json(200, []),
  });
}

beforeEach(() => {
  setAccessToken("access");
  cancelPendingSync();
  useReservations.getState().clear();
  useMachines.setState({
    center: { lat: 44.44, lng: 26.08 },
    source: "demo",
    nearby: { status: "ready", data: [summary()] },
    details: { [MACHINE_ID]: { status: "ready", data: detail() } },
  });
});

it("re-reads a machine on screen once for a burst of notices about it", async () => {
  const { seen } = serve();

  for (let i = 0; i < 3; i++)
    applyNotice(notice({ machine_id: MACHINE_ID, change: "availability" }));
  applyNotice(notice({ topic: "machine", machine_id: MACHINE_ID, change: "offline" }));
  await settle();

  const paths = seen.map((s) => s.path);
  expect(paths.filter((p) => p === `/api/v1/machines/${MACHINE_ID}`)).toHaveLength(1);
  expect(paths.filter((p) => p === "/api/v1/machines/nearby")).toHaveLength(1);
  expect(useMachines.getState().details[MACHINE_ID]?.data?.online).toBe(false);
});

it("doesn't fetch machines the app isn't showing", async () => {
  const { seen } = serve();

  applyNotice(notice({ machine_id: OTHER_MACHINE, change: "availability" }));
  await settle();

  expect(seen.map((s) => s.path)).toEqual(["/api/v1/machines/nearby"]);
});

it("re-reads my reservation when a notice names it", async () => {
  const { seen } = serve();
  useReservations.setState({ byId: { [RESERVATION_ID]: reservation({ status: "paid" }) } });

  applyNotice(notice({ topic: "reservation", reservation_id: RESERVATION_ID, change: "expired" }));
  await settle();

  expect(seen.map((s) => s.path)).toEqual([`/api/v1/reservations/${RESERVATION_ID}`]);
  expect(useReservations.getState().byId[RESERVATION_ID]?.status).toBe("expired");
});

it("re-reads everything on screen after (re)connecting", async () => {
  const { seen } = serve();
  useReservations.setState({
    byId: { [RESERVATION_ID]: reservation({ status: "held" }) },
    orders: { status: "ready", data: [] },
  });

  applyNotice(notice({ topic: "hello", change: "subscribed" }));
  await settle();

  expect(new Set(seen.map((s) => s.path))).toEqual(
    new Set([
      "/api/v1/machines/nearby",
      `/api/v1/machines/${MACHINE_ID}`,
      `/api/v1/reservations/${RESERVATION_ID}`,
      "/api/v1/orders",
    ]),
  );
});

it("drops pending re-reads when stopped", async () => {
  const { seen } = serve();

  applyNotice(notice({ machine_id: MACHINE_ID, change: "availability" }));
  cancelPendingSync();
  await settle();

  expect(seen).toHaveLength(0);
});
