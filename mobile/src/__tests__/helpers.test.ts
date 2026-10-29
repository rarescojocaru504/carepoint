import { formatDistance, formatPrice } from "../format";
import { machineState } from "../machineState";

describe("formatPrice", () => {
  it("shows integer cents as a decimal amount", () => {
    expect(formatPrice(1250, "RON")).toBe("12.50 RON");
    expect(formatPrice(5, "RON")).toBe("0.05 RON");
  });
});

describe("formatDistance", () => {
  it("uses metres up to a kilometre, then kilometres", () => {
    expect(formatDistance(347)).toBe("350 m");
    expect(formatDistance(999)).toBe("1000 m");
    expect(formatDistance(1234)).toBe("1.2 km");
  });
});

describe("machineState", () => {
  const base = { status: "active" as const, online: true, units_available: 4 };

  it("is available when active, online and stocked", () => {
    expect(machineState(base)).toBe("available");
  });

  it("puts maintenance before offline, and offline before sold out", () => {
    expect(machineState({ ...base, status: "maintenance", online: false })).toBe("maintenance");
    expect(machineState({ ...base, online: false, units_available: 0 })).toBe("offline");
    expect(machineState({ ...base, units_available: 0 })).toBe("soldOut");
  });
});
