import { useEffect, useState } from "react";
import { StyleSheet, Text, View } from "react-native";

import { formatCountdown, secondsLeft, serverNow } from "../services/clock";
import { colors } from "./ui";

/** Seconds until `expiresAt` on the server's clock, updated every second. */
export function useSecondsLeft(expiresAt: string): number {
  const [now, setNow] = useState(serverNow);
  useEffect(() => {
    const timer = setInterval(() => setNow(serverNow()), 1000);
    return () => clearInterval(timer);
  }, []);
  return secondsLeft(expiresAt, now);
}

export function Countdown({ expiresAt, label }: { expiresAt: string; label: string }) {
  const seconds = useSecondsLeft(expiresAt);
  const urgent = seconds < 120;
  return (
    <View
      style={styles.box}
      accessibilityRole="timer"
      accessibilityLabel={`${label}: ${Math.ceil(seconds / 60)} minutes left`}
    >
      <Text style={styles.label}>{label}</Text>
      <Text style={[styles.time, urgent && styles.urgent]}>{formatCountdown(seconds)}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  box: { alignItems: "center", paddingVertical: 8 },
  label: { color: colors.muted, fontSize: 14 },
  time: {
    fontSize: 44,
    fontWeight: "700",
    color: colors.text,
    fontVariant: ["tabular-nums"],
  },
  urgent: { color: colors.danger },
});
