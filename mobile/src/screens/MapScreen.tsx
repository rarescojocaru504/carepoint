import { useEffect, useMemo } from "react";
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from "react-native";
import MapView, { Marker } from "react-native-maps";

import { ActiveReservations } from "../components/ActiveReservations";
import { Banner, Button, Centered, colors, StateBadge } from "../components/ui";
import { formatDistance } from "../format";
import { machineState, STATE_COLOR } from "../machineState";
import type { ScreenProps } from "../navigation";
import { DEMO_CENTER, SEARCH_RADIUS_M, useMachines } from "../store/machines";
import type { MachineSummary } from "../store/machines";
import { useReservations } from "../store/reservations";

// About 15 km across: the demo city at a glance.
const REGION_DELTA = 0.15;

export function MapScreen({ navigation }: ScreenProps<"Map">) {
  const center = useMachines((s) => s.center);
  const source = useMachines((s) => s.source);
  const nearby = useMachines((s) => s.nearby);
  const locate = useMachines((s) => s.locate);
  const loadNearby = useMachines((s) => s.loadNearby);
  const loadMine = useReservations((s) => s.loadMine);

  useEffect(() => {
    void locate();
    void loadMine(); // shows any holds still running, e.g. after an app restart
  }, [locate, loadMine]);

  const region = useMemo(
    () =>
      center === null
        ? null
        : {
            latitude: center.lat,
            longitude: center.lng,
            latitudeDelta: REGION_DELTA,
            longitudeDelta: REGION_DELTA,
          },
    [center],
  );

  const machines = nearby.data ?? [];
  const open = (machine: MachineSummary) =>
    navigation.navigate("Machine", { machineId: machine.id, name: machine.name });
  const refresh = () => {
    if (center !== null && source !== null) void loadNearby(center, source);
  };

  if (region === null) {
    return (
      <Centered>
        <Text style={styles.muted}>Finding machines near you…</Text>
      </Centered>
    );
  }

  return (
    <View style={styles.screen}>
      <MapView style={styles.map} region={region} showsUserLocation={source === "device"}>
        {machines.map((machine) => (
          <Marker
            key={machine.id}
            coordinate={{ latitude: machine.location.lat, longitude: machine.location.lng }}
            title={machine.name}
            description={`${machine.units_available} items available`}
            pinColor={STATE_COLOR[machineState(machine)]}
            onCalloutPress={() => open(machine)}
          />
        ))}
      </MapView>

      <FlatList
        style={styles.list}
        data={machines}
        keyExtractor={(machine) => machine.id}
        refreshControl={
          <RefreshControl refreshing={nearby.status === "loading"} onRefresh={refresh} />
        }
        ListHeaderComponent={
          <View style={styles.notices}>
            <ActiveReservations
              onOpen={(reservationId) => navigation.navigate("Reservation", { reservationId })}
            />
            {nearby.status === "error" && <Banner tone="error">{nearby.error}</Banner>}
            {source === "demo" && (
              <Banner tone="info">
                Showing the Bucharest demo area. Allow location access to see machines near you.
              </Banner>
            )}
          </View>
        }
        ListEmptyComponent={
          nearby.status === "ready" ? (
            <View style={styles.empty}>
              <Text style={styles.muted}>
                No CarePoint machines within {SEARCH_RADIUS_M / 1000} km.
              </Text>
              {source === "device" && (
                <Button
                  title="Show the Bucharest demo area"
                  variant="secondary"
                  onPress={() => void loadNearby(DEMO_CENTER, "demo")}
                />
              )}
            </View>
          ) : undefined
        }
        renderItem={({ item }) => <MachineRow machine={item} onPress={() => open(item)} />}
      />
    </View>
  );
}

function MachineRow({ machine, onPress }: { machine: MachineSummary; onPress: () => void }) {
  const state = machineState(machine);
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`${machine.name}, ${formatDistance(machine.distance_m)} away`}
      onPress={onPress}
      style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
    >
      <View style={styles.rowMain}>
        <Text style={styles.name}>{machine.name}</Text>
        <Text style={styles.muted}>{machine.address}</Text>
        <Text style={styles.muted}>
          {formatDistance(machine.distance_m)} · {machine.units_available} items ·{" "}
          {machine.products_available} products
        </Text>
      </View>
      <StateBadge state={state} />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background },
  map: { height: "45%" },
  list: { flex: 1 },
  notices: { padding: 12, gap: 8 },
  empty: { padding: 24, gap: 12, alignItems: "center" },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    padding: 16,
    backgroundColor: "#fff",
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.border,
  },
  rowPressed: { backgroundColor: colors.background },
  rowMain: { flex: 1, gap: 2 },
  name: { fontSize: 16, fontWeight: "600", color: colors.text },
  muted: { color: colors.muted, fontSize: 14, textAlign: "left" },
});
