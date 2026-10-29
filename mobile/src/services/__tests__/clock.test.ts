import { installFakeServer } from "../../test-utils/fakeServer";
import { api } from "../api";
import {
  formatCountdown,
  noteServerDate,
  resetServerClock,
  secondsLeft,
  serverNow,
} from "../clock";

afterEach(resetServerClock);

describe("server clock", () => {
  it("follows the server's Date header, not the phone's clock", () => {
    const phone = Date.parse("2026-10-29T12:00:00Z");
    noteServerDate("Thu, 29 Oct 2026 12:02:00 GMT", phone); // phone is 2 minutes slow
    const expires = "2026-10-29T12:17:00Z";
    expect(secondsLeft(expires, phone + 120_000)).toBe(15 * 60);
    expect(Math.round((serverNow() - Date.now()) / 1000)).toBe(120);
  });

  it("ignores a missing or unreadable header", () => {
    noteServerDate(null);
    noteServerDate("not a date");
    expect(Math.abs(serverNow() - Date.now())).toBeLessThan(50);
  });

  it("reads the Date header of every API response", async () => {
    const ahead = new Date(Date.now() + 60 * 60_000).toUTCString();
    installFakeServer({
      "GET /health": () =>
        new Response("{}", {
          status: 200,
          headers: { "Content-Type": "application/json", Date: ahead },
        }),
    });

    await api.GET("/health");

    expect(Math.round((serverNow() - Date.now()) / 60_000)).toBe(60);
  });
});

describe("countdown", () => {
  it("never goes below zero and shows minutes and seconds", () => {
    expect(secondsLeft("2026-10-29T12:00:00Z", Date.parse("2026-10-29T12:05:00Z"))).toBe(0);
    expect(formatCountdown(900)).toBe("15:00");
    expect(formatCountdown(754)).toBe("12:34");
    expect(formatCountdown(9)).toBe("0:09");
  });
});
