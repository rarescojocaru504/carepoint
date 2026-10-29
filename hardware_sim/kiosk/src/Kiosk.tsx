import { useCallback, useEffect, useRef, useState } from "react";

import {
  type DispenseOutcome,
  type MachineEvent,
  type MachineSnapshot,
  type Refusal,
  subscribe,
  verifyCode,
} from "./api";
import { CODE_LENGTH, CodeDisplay, Keypad } from "./components/Keypad";
import { OperatorPanel } from "./components/OperatorPanel";
import { QrScanner } from "./components/QrScanner";
import { PickupTray, SlotGrid } from "./components/SlotGrid";

const RESET_AFTER_MS = 8000;

type Phase =
  | { kind: "idle"; notice: string | null }
  | { kind: "verifying" }
  | { kind: "dispensing"; slot: string | null; quantity: number; units: number }
  | { kind: "done"; slot: string; units: number }
  | { kind: "failed"; slot: string; result: DispenseOutcome }
  | { kind: "locked"; until: number };

const REFUSALS: Record<Refusal, string> = {
  invalid_code: "Code not recognised. Check the code in the app and try again.",
  too_many_attempts: "Too many wrong codes.",
  pickup_in_progress: "This order is already being dispensed.",
  machine_unavailable: "This machine is out of service right now.",
  offline: "The machine is offline. Please try again in a moment.",
  busy: "Please wait, the machine is still dispensing.",
};

function failureMessage(result: DispenseOutcome): string {
  if (result.units_dispensed === 0) {
    return "Sorry, nothing came out. Your order is still valid: enter the same code to try again.";
  }
  return `Only ${result.units_dispensed} item(s) came out. Your order was cancelled and you will not be charged.`;
}

export function Kiosk({ serial }: { serial: string }) {
  const [machine, setMachine] = useState<MachineSnapshot | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: "idle", notice: null });
  const [code, setCode] = useState("");
  const [scanning, setScanning] = useState(false);
  const [now, setNow] = useState(Date.now());
  const reset = useRef<number | undefined>(undefined);

  const backToIdleLater = (): void => {
    window.clearTimeout(reset.current);
    reset.current = window.setTimeout(() => setPhase({ kind: "idle", notice: null }), RESET_AFTER_MS);
  };

  useEffect(
    () =>
      subscribe(serial, (event: MachineEvent) => {
        setMachine(event.machine);
        switch (event.event) {
          case "dispensing":
            setPhase({ kind: "dispensing", slot: event.slot, quantity: event.quantity, units: 0 });
            break;
          case "unit_dispensed":
            setPhase({ kind: "dispensing", slot: event.slot, quantity: event.quantity, units: event.unit });
            break;
          case "dispensed":
            setPhase(
              event.result.outcome === "dispensed"
                ? { kind: "done", slot: event.slot, units: event.result.units_dispensed }
                : { kind: "failed", slot: event.slot, result: event.result },
            );
            backToIdleLater();
            break;
          default:
            break;
        }
      }),
    [serial],
  );

  // Countdown while the keypad is locked after too many wrong codes.
  useEffect(() => {
    if (phase.kind !== "locked") return undefined;
    const timer = window.setInterval(() => {
      setNow(Date.now());
      if (Date.now() >= phase.until) setPhase({ kind: "idle", notice: null });
    }, 250);
    return () => window.clearInterval(timer);
  }, [phase]);

  const submit = useCallback(
    async (credential: string): Promise<void> => {
      setScanning(false);
      setPhase({ kind: "verifying" });
      let result;
      try {
        result = await verifyCode(serial, credential);
      } catch {
        result = { ok: false as const, refusal: "offline" as const };
      }
      setCode("");
      if (result.ok) {
        // The dispense command follows; its events move the screen on. Keep
        // "verifying" unless the dispensing event already arrived.
        setPhase((current) =>
          current.kind === "verifying" ? { kind: "dispensing", slot: null, quantity: 0, units: 0 } : current,
        );
      } else if (result.refusal === "too_many_attempts") {
        setPhase({ kind: "locked", until: Date.now() + (result.retry_after_seconds ?? 60) * 1000 });
      } else {
        setPhase({ kind: "idle", notice: REFUSALS[result.refusal] });
      }
    },
    [serial],
  );

  const onCode = (next: string): void => {
    setCode(next);
    if (phase.kind === "idle" && phase.notice !== null) setPhase({ kind: "idle", notice: null });
    if (next.length === CODE_LENGTH) void submit(next);
  };

  if (machine === null) {
    return <div className="loading">Connecting to {serial}…</div>;
  }

  const idle = phase.kind === "idle";
  const keypadDisabled = !idle || !machine.online;
  const activeSlot =
    phase.kind === "dispensing" || phase.kind === "done" || phase.kind === "failed" ? phase.slot : null;
  const trayUnits =
    phase.kind === "dispensing" || phase.kind === "done"
      ? phase.units
      : phase.kind === "failed"
        ? phase.result.units_dispensed
        : 0;

  return (
    <div className="kiosk">
      <header className="kiosk-header">
        <div className="brand">
          Care<span>Point</span>
        </div>
        <div className="serial">{machine.serial}</div>
        <div className={`status ${machine.online ? "online" : "offline"}`} data-testid="status">
          {machine.online ? "Online" : "Offline"}
        </div>
      </header>

      <main className="kiosk-main">
        <section className="panel entry" aria-live="polite">
          <Screen phase={phase} now={now} online={machine.online} />
          <CodeDisplay code={code} shake={idle && phase.notice !== null} />
          <Keypad code={code} disabled={keypadDisabled} onChange={onCode} />
          <button type="button" className="button scan" disabled={keypadDisabled} onClick={() => setScanning(true)}>
            Scan QR code instead
          </button>
        </section>

        <section className="panel window" aria-label="Machine slots">
          <SlotGrid slots={machine.slots} active={activeSlot} />
          <PickupTray units={trayUnits} />
        </section>
      </main>

      {scanning && <QrScanner onScan={(payload) => void submit(payload)} onClose={() => setScanning(false)} />}
      <OperatorPanel machine={machine} />
    </div>
  );
}

function Screen({ phase, now, online }: { phase: Phase; now: number; online: boolean }) {
  switch (phase.kind) {
    case "idle":
      return (
        <div className="screen">
          <h1>{online ? "Enter your pickup code" : "Machine offline"}</h1>
          <p className={phase.notice ? "notice" : "muted"} data-testid="notice">
            {phase.notice ?? (online ? "The 6-digit code is in the CarePoint app." : "Reconnecting…")}
          </p>
        </div>
      );
    case "verifying":
      return (
        <div className="screen">
          <h1>Checking your code…</h1>
          <p className="muted">One moment.</p>
        </div>
      );
    case "dispensing":
      return (
        <div className="screen">
          <h1>Dispensing{phase.slot ? ` from ${phase.slot}` : ""}…</h1>
          <p className="muted" data-testid="progress">
            {phase.quantity > 0 ? `${phase.units} of ${phase.quantity}` : "Starting the mechanism"}
          </p>
          <div className="spinner" aria-hidden="true" />
        </div>
      );
    case "done":
      return (
        <div className="screen success">
          <h1 data-testid="done">Please take your item</h1>
          <p className="muted">Thank you for using CarePoint.</p>
        </div>
      );
    case "failed":
      return (
        <div className="screen failure">
          <h1>Something went wrong</h1>
          <p className="notice">{failureMessage(phase.result)}</p>
        </div>
      );
    case "locked":
      return (
        <div className="screen failure">
          <h1>Too many wrong codes</h1>
          <p className="notice">Please wait {Math.max(0, Math.ceil((phase.until - now) / 1000))} s.</p>
        </div>
      );
  }
}
