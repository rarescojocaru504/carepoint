import { useEffect } from "react";

export const CODE_LENGTH = 6;

interface KeypadProps {
  code: string;
  disabled: boolean;
  onChange: (code: string) => void;
}

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "clear", "0", "back"] as const;

/** On-screen keypad; the physical keyboard works too (digits, Backspace, Escape). */
export function Keypad({ code, disabled, onChange }: KeypadProps) {
  const press = (key: (typeof KEYS)[number]): void => {
    if (disabled) return;
    if (key === "clear") onChange("");
    else if (key === "back") onChange(code.slice(0, -1));
    else if (code.length < CODE_LENGTH) onChange(code + key);
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.target instanceof HTMLInputElement) return; // typing in the QR field
      if (/^[0-9]$/.test(event.key)) press(event.key as (typeof KEYS)[number]);
      else if (event.key === "Backspace") press("back");
      else if (event.key === "Escape") press("clear");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <div className="keypad" role="group" aria-label="Keypad">
      {KEYS.map((key) => (
        <button
          key={key}
          type="button"
          className={`key key-${key.length > 1 ? key : "digit"}`}
          disabled={disabled}
          onClick={() => press(key)}
          aria-label={key === "back" ? "Delete last digit" : key === "clear" ? "Clear" : key}
        >
          {key === "back" ? "⌫" : key === "clear" ? "C" : key}
        </button>
      ))}
    </div>
  );
}

export function CodeDisplay({ code, shake }: { code: string; shake: boolean }) {
  return (
    <div className={`code-display${shake ? " shake" : ""}`} aria-label="Pickup code" data-testid="code">
      {Array.from({ length: CODE_LENGTH }, (_, i) => (
        <span key={i} className={`code-cell${i < code.length ? " filled" : ""}`}>
          {code[i] ?? ""}
        </span>
      ))}
    </div>
  );
}
