import { StyleSheet, Text, View } from "react-native";
import QRCode from "react-native-qrcode-svg";

import type { Pickup } from "../store/reservations";
import { colors } from "./ui";

/** The pickup credential (ADR-016): a QR for the kiosk camera and the 6-digit
 * code to type. Rendered from memory only; never saved (ADR-027). */
export function PickupCode({ pickup }: { pickup: Pickup }) {
  const spaced = `${pickup.code.slice(0, 3)} ${pickup.code.slice(3)}`;
  return (
    <View style={styles.box}>
      <View style={styles.qr} accessibilityLabel="Pickup QR code">
        <QRCode value={pickup.qr_payload} size={200} />
      </View>
      <Text style={styles.label}>or type this code at the machine</Text>
      <Text
        style={styles.code}
        selectable={false}
        accessibilityLabel={`Pickup code ${pickup.code.split("").join(" ")}`}
      >
        {spaced}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  box: { alignItems: "center", gap: 8, paddingVertical: 8 },
  qr: { padding: 12, backgroundColor: "#fff", borderRadius: 12 },
  label: { color: colors.muted, fontSize: 14 },
  code: {
    fontSize: 40,
    fontWeight: "700",
    letterSpacing: 6,
    color: colors.text,
    fontVariant: ["tabular-nums"],
  },
});
