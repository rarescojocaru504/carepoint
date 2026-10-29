import { useEffect } from "react";
import { FlatList, RefreshControl, StyleSheet, Text, View } from "react-native";

import { Banner, Centered, colors, StateBadge } from "../components/ui";
import { formatPrice } from "../format";
import { machineState } from "../machineState";
import type { ScreenProps } from "../navigation";
import type { Schemas } from "../services/api";
import { useMachines } from "../store/machines";

type Slot = Schemas["SlotAvailability"];

export function MachineScreen({ route }: ScreenProps<"Machine">) {
  const { machineId } = route.params;
  const detail = useMachines((s) => s.details[machineId]);
  const loadMachine = useMachines((s) => s.loadMachine);

  useEffect(() => {
    void loadMachine(machineId);
  }, [loadMachine, machineId]);

  const machine = detail?.data ?? null;
  if (machine === null) {
    return (
      <Centered>
        {detail?.status === "error" ? (
          <Banner tone="error">{detail.error}</Banner>
        ) : (
          <Text style={styles.muted}>Loading…</Text>
        )}
      </Centered>
    );
  }

  const units = machine.slots.reduce((sum, slot) => sum + slot.available, 0);
  const state = machineState({ ...machine, units_available: units });

  return (
    <FlatList
      style={styles.screen}
      data={machine.slots}
      keyExtractor={(slot) => slot.slot_id}
      refreshControl={
        <RefreshControl
          refreshing={detail?.status === "loading"}
          onRefresh={() => void loadMachine(machineId)}
        />
      }
      ListHeaderComponent={
        <View style={styles.header}>
          <Text style={styles.title}>{machine.name}</Text>
          <Text style={styles.muted}>{machine.address}</Text>
          <StateBadge state={state} />
          {detail?.status === "error" && <Banner tone="error">{detail.error}</Banner>}
          {state === "offline" && (
            <Banner tone="error">
              This machine is offline right now, so nothing can be picked up here until it
              reconnects.
            </Banner>
          )}
          {state === "maintenance" && (
            <Banner tone="info">This machine is under maintenance.</Banner>
          )}
        </View>
      }
      ListEmptyComponent={<Text style={styles.empty}>Nothing in stock here right now.</Text>}
      renderItem={({ item }) => <SlotRow slot={item} />}
    />
  );
}

function SlotRow({ slot }: { slot: Slot }) {
  const soldOut = slot.available === 0;
  return (
    <View style={styles.row} accessibilityLabel={`${slot.product.name}, ${slot.available} left`}>
      <View style={styles.rowMain}>
        <Text style={styles.name}>{slot.product.name}</Text>
        {slot.product.description !== null && (
          <Text style={styles.muted} numberOfLines={2}>
            {slot.product.description}
          </Text>
        )}
        <Text style={styles.price}>
          {formatPrice(slot.product.price_cents, slot.product.currency)}
        </Text>
      </View>
      <View style={styles.stock}>
        <Text style={[styles.count, soldOut && styles.soldOut]}>
          {soldOut ? "Sold out" : `${slot.available} left`}
        </Text>
        {slot.low_stock && !soldOut && <Text style={styles.low}>Low stock</Text>}
        <Text style={styles.slotCode}>Slot {slot.slot_code}</Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background },
  header: { padding: 16, gap: 8, backgroundColor: "#fff" },
  title: { fontSize: 22, fontWeight: "700", color: colors.text },
  muted: { color: colors.muted, fontSize: 14 },
  empty: { padding: 24, textAlign: "center", color: colors.muted },
  row: {
    flexDirection: "row",
    gap: 12,
    padding: 16,
    backgroundColor: "#fff",
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.border,
  },
  rowMain: { flex: 1, gap: 4 },
  name: { fontSize: 16, fontWeight: "600", color: colors.text },
  price: { fontSize: 15, color: colors.text, fontWeight: "500" },
  stock: { alignItems: "flex-end", gap: 2 },
  count: { fontSize: 15, fontWeight: "600", color: colors.primary },
  soldOut: { color: colors.muted },
  low: { fontSize: 12, color: colors.warning, fontWeight: "600" },
  slotCode: { fontSize: 12, color: colors.muted },
});
