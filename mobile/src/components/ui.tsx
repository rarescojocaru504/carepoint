// Small shared building blocks, so the screens look consistent.

import type { ReactNode } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { STATE_COLOR, STATE_LABEL } from "../machineState";
import type { MachineState } from "../machineState";

export const colors = {
  primary: "#0f766e",
  text: "#0f172a",
  muted: "#64748b",
  border: "#e2e8f0",
  background: "#f8fafc",
  danger: "#b91c1c",
  warning: "#b45309",
};

export function Button(props: {
  title: string;
  onPress: () => void;
  disabled?: boolean;
  busy?: boolean;
  variant?: "primary" | "secondary";
}) {
  const secondary = props.variant === "secondary";
  const disabled = props.disabled === true || props.busy === true;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled, busy: props.busy === true }}
      disabled={disabled}
      onPress={props.onPress}
      style={({ pressed }) => [
        styles.button,
        secondary && styles.buttonSecondary,
        (pressed || disabled) && styles.buttonDimmed,
      ]}
    >
      {props.busy === true ? (
        <ActivityIndicator color={secondary ? colors.primary : "#fff"} />
      ) : (
        <Text style={[styles.buttonText, secondary && styles.buttonTextSecondary]}>
          {props.title}
        </Text>
      )}
    </Pressable>
  );
}

export function StateBadge({ state }: { state: MachineState }) {
  return (
    <View style={[styles.badge, { backgroundColor: STATE_COLOR[state] }]}>
      <Text style={styles.badgeText}>{STATE_LABEL[state]}</Text>
    </View>
  );
}

export function Banner(props: { tone: "error" | "info"; children: ReactNode }) {
  return (
    <View
      accessibilityRole="alert"
      style={[styles.banner, props.tone === "error" ? styles.bannerError : styles.bannerInfo]}
    >
      <Text style={styles.bannerText}>{props.children}</Text>
    </View>
  );
}

export function Centered({ children }: { children: ReactNode }) {
  return <View style={styles.centered}>{children}</View>;
}

const styles = StyleSheet.create({
  button: {
    backgroundColor: colors.primary,
    borderRadius: 10,
    paddingVertical: 14,
    paddingHorizontal: 20,
    alignItems: "center",
  },
  buttonSecondary: {
    backgroundColor: "transparent",
    borderWidth: 1,
    borderColor: colors.primary,
  },
  buttonDimmed: { opacity: 0.6 },
  buttonText: { color: "#fff", fontWeight: "600", fontSize: 16 },
  buttonTextSecondary: { color: colors.primary },
  badge: { borderRadius: 999, paddingHorizontal: 10, paddingVertical: 3, alignSelf: "flex-start" },
  badgeText: { color: "#fff", fontSize: 12, fontWeight: "600" },
  banner: { padding: 12, borderRadius: 8 },
  bannerError: { backgroundColor: "#fee2e2" },
  bannerInfo: { backgroundColor: "#e0f2fe" },
  bannerText: { color: colors.text, fontSize: 14 },
  centered: { flex: 1, alignItems: "center", justifyContent: "center", padding: 24, gap: 12 },
});
