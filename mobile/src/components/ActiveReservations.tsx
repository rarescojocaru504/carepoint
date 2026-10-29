import { Pressable, StyleSheet, Text, View } from "react-native";

import { formatCountdown } from "../services/clock";
import { isActive, useReservations } from "../store/reservations";
import type { Reservation } from "../store/reservations";
import { useSecondsLeft } from "./Countdown";
import { colors } from "./ui";

/** The customer's live holds, soonest first, each with its countdown. A hold
 * that expires or is picked up disappears as soon as the app hears of it. */
export function ActiveReservations({ onOpen }: { onOpen: (reservationId: string) => void }) {
  const byId = useReservations((s) => s.byId);
  const active = Object.values(byId)
    .filter(isActive)
    .sort((a, b) => Date.parse(a.expires_at) - Date.parse(b.expires_at));
  if (active.length === 0) return null;
  return (
    <View style={styles.list}>
      {active.map((reservation) => (
        <Row
          key={reservation.id}
          reservation={reservation}
          onPress={() => onOpen(reservation.id)}
        />
      ))}
    </View>
  );
}

function Row({ reservation, onPress }: { reservation: Reservation; onPress: () => void }) {
  const seconds = useSecondsLeft(reservation.expires_at);
  const action = reservation.status === "paid" ? "Pick up within" : "Pay within";
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`Your reservation: ${reservation.product_name}. ${action} ${Math.ceil(seconds / 60)} minutes.`}
      onPress={onPress}
      style={({ pressed }) => [styles.row, pressed && styles.pressed]}
    >
      <View style={styles.main}>
        <Text style={styles.title}>{reservation.product_name}</Text>
        <Text style={styles.detail}>
          {action} {formatCountdown(seconds)}
        </Text>
      </View>
      <Text style={styles.chevron}>›</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  list: { gap: 8 },
  row: {
    flexDirection: "row",
    alignItems: "center",
    padding: 12,
    borderRadius: 10,
    backgroundColor: colors.primary,
  },
  pressed: { opacity: 0.85 },
  main: { flex: 1, gap: 2 },
  title: { color: "#fff", fontWeight: "600", fontSize: 15 },
  detail: { color: "#fff", fontSize: 14, fontVariant: ["tabular-nums"] },
  chevron: { color: "#fff", fontSize: 24, paddingLeft: 8 },
});
