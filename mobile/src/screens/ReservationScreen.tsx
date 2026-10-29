import { useEffect, useState } from "react";
import { Alert, Pressable, RefreshControl, ScrollView, StyleSheet, Text, View } from "react-native";

import { Countdown, useSecondsLeft } from "../components/Countdown";
import { PickupCode } from "../components/PickupCode";
import { Banner, Button, Centered, colors } from "../components/ui";
import { formatPrice } from "../format";
import { friendlyError } from "../messages";
import type { ScreenProps } from "../navigation";
import { useMachines } from "../store/machines";
import { DECLINED_CARD, isActive, TEST_CARD, useReservations } from "../store/reservations";
import type { Reservation } from "../store/reservations";

export function ReservationScreen({ route, navigation }: ScreenProps<"Reservation">) {
  const { reservationId } = route.params;
  const reservation = useReservations((s) => s.byId[reservationId]);
  const load = useReservations((s) => s.load);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    load(reservationId).catch((e: unknown) => setError(friendlyError(e)));
  }, [load, reservationId]);

  const refresh = () => {
    setRefreshing(true);
    load(reservationId)
      .then(() => setError(null))
      .catch((e: unknown) => setError(friendlyError(e)))
      .finally(() => setRefreshing(false));
  };

  if (reservation === undefined) {
    return (
      <Centered>
        {error !== null ? <Banner tone="error">{error}</Banner> : <Text>Loading…</Text>}
      </Centered>
    );
  }

  return (
    <ScrollView
      style={styles.screen}
      contentContainerStyle={styles.content}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={refresh} />}
    >
      <Summary reservation={reservation} />
      <MachineWarning reservation={reservation} />
      {error !== null && <Banner tone="error">{error}</Banner>}
      <Body
        reservation={reservation}
        onError={setError}
        onBackToMachine={(machineId, name) => navigation.navigate("Machine", { machineId, name })}
      />
    </ScrollView>
  );
}

function Summary({ reservation }: { reservation: Reservation }) {
  const machine = useMachineName(reservation.machine_id);
  return (
    <View style={styles.card}>
      <Text style={styles.product}>
        {reservation.quantity > 1 ? `${reservation.quantity} × ` : ""}
        {reservation.product_name}
      </Text>
      {machine !== null && <Text style={styles.muted}>{machine}</Text>}
      <Text style={styles.price}>{formatPrice(reservation.total_cents, reservation.currency)}</Text>
    </View>
  );
}

/** While a hold is active, say so if its machine can't hand the item out now.
 * The machine's detail is loaded here, so live notices keep it current. */
function MachineWarning({ reservation }: { reservation: Reservation }) {
  const machine = useMachines((s) => s.details[reservation.machine_id]?.data);
  const loadMachine = useMachines((s) => s.loadMachine);
  useEffect(() => {
    void loadMachine(reservation.machine_id);
  }, [loadMachine, reservation.machine_id]);
  if (!machine || !isActive(reservation)) return null;
  if (machine.status !== "active") {
    return (
      <Banner tone="error">
        This machine has been taken out of service. Cancel to free your hold; you won&apos;t be
        charged.
      </Banner>
    );
  }
  if (!machine.online) {
    return (
      <Banner tone="error">
        This machine is offline right now. Your hold stays until the timer runs out, and you&apos;re
        only charged if the machine hands you the item.
      </Banner>
    );
  }
  return null;
}

/** The machine's name, from what the app already loaded, else fetched once. */
function useMachineName(machineId: string): string | null {
  const fromDetail = useMachines((s) => s.details[machineId]?.data?.name);
  const fromNearby = useMachines((s) => s.nearby.data?.find((m) => m.id === machineId)?.name);
  const loadMachine = useMachines((s) => s.loadMachine);
  const known = fromDetail ?? fromNearby ?? null;
  useEffect(() => {
    if (known === null) void loadMachine(machineId);
  }, [known, loadMachine, machineId]);
  return known;
}

interface BodyProps {
  reservation: Reservation;
  onError: (message: string | null) => void;
  onBackToMachine: (machineId: string, name: string) => void;
}

function Body({ reservation, onError, onBackToMachine }: BodyProps) {
  switch (reservation.status) {
    case "held":
      return <Held reservation={reservation} onError={onError} />;
    case "paid":
      return <Paid reservation={reservation} onError={onError} />;
    case "dispensed":
      return <Banner tone="info">Picked up. Thank you for using CarePoint!</Banner>;
    case "cancelled":
      return <Banner tone="info">Cancelled. You haven&apos;t been charged.</Banner>;
    case "expired":
      return (
        <View style={styles.section}>
          <Banner tone="info">
            This hold ran out, so the item went back on sale. You haven&apos;t been charged.
          </Banner>
          <MachineButton reservation={reservation} onPress={onBackToMachine} />
        </View>
      );
  }
}

function MachineButton({
  reservation,
  onPress,
}: {
  reservation: Reservation;
  onPress: BodyProps["onBackToMachine"];
}) {
  const name = useMachineName(reservation.machine_id) ?? "Machine";
  return <Button title="Reserve again" onPress={() => onPress(reservation.machine_id, name)} />;
}

/** When the countdown reaches zero, ask the server: it reads a lapsed hold as expired. */
function useReloadAtExpiry(reservation: Reservation) {
  const seconds = useSecondsLeft(reservation.expires_at);
  const load = useReservations((s) => s.load);
  useEffect(() => {
    if (seconds === 0) void load(reservation.id).catch(() => undefined);
  }, [seconds, load, reservation.id]);
}

function Held({
  reservation,
  onError,
}: {
  reservation: Reservation;
  onError: BodyProps["onError"];
}) {
  const pay = useReservations((s) => s.pay);
  const [busy, setBusy] = useState(false);
  useReloadAtExpiry(reservation);

  const checkout = async (card: string) => {
    setBusy(true);
    onError(null);
    try {
      await pay(reservation.id, card);
    } catch (e) {
      onError(friendlyError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.section}>
      <Countdown expiresAt={reservation.expires_at} label="Held for you" />
      <Text style={styles.muted}>
        Pay now to get your pickup code. Nothing is charged until the machine hands you the item.
      </Text>
      <Button
        title={`Pay ${formatPrice(reservation.total_cents, reservation.currency)}`}
        busy={busy}
        onPress={() => void checkout(TEST_CARD)}
      />
      <Pressable
        accessibilityRole="button"
        disabled={busy}
        onPress={() => void checkout(DECLINED_CARD)}
      >
        <Text style={styles.link}>Demo: try a declined card</Text>
      </Pressable>
      <CancelButton reservation={reservation} onError={onError} />
    </View>
  );
}

function Paid({
  reservation,
  onError,
}: {
  reservation: Reservation;
  onError: BodyProps["onError"];
}) {
  const pickup = useReservations((s) => s.pickups[reservation.id]);
  const newPickupCode = useReservations((s) => s.newPickupCode);
  const [busy, setBusy] = useState(false);
  useReloadAtExpiry(reservation);

  const reveal = async () => {
    setBusy(true);
    onError(null);
    try {
      await newPickupCode(reservation.id);
    } catch (e) {
      onError(friendlyError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.section}>
      <Countdown expiresAt={reservation.expires_at} label="Pick up within" />
      {pickup !== undefined ? (
        <PickupCode pickup={pickup} />
      ) : (
        <View style={styles.section}>
          <Text style={styles.muted}>
            For your safety the code isn&apos;t stored on this phone. Get a new one; any earlier
            code stops working.
          </Text>
          <Button title="Show my pickup code" busy={busy} onPress={() => void reveal()} />
        </View>
      )}
      <CancelButton reservation={reservation} onError={onError} />
    </View>
  );
}

function CancelButton({
  reservation,
  onError,
}: {
  reservation: Reservation;
  onError: BodyProps["onError"];
}) {
  const cancel = useReservations((s) => s.cancel);
  const [busy, setBusy] = useState(false);

  const confirm = () =>
    Alert.alert("Cancel this reservation?", "The item goes back on sale and you aren't charged.", [
      { text: "Keep it", style: "cancel" },
      {
        text: "Cancel reservation",
        style: "destructive",
        onPress: () => {
          setBusy(true);
          onError(null);
          cancel(reservation.id)
            .catch((e: unknown) => onError(friendlyError(e)))
            .finally(() => setBusy(false));
        },
      },
    ]);

  return <Button title="Cancel reservation" variant="secondary" busy={busy} onPress={confirm} />;
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background },
  content: { padding: 16, gap: 16 },
  card: { backgroundColor: "#fff", borderRadius: 12, padding: 16, gap: 4 },
  product: { fontSize: 20, fontWeight: "700", color: colors.text },
  price: { fontSize: 16, fontWeight: "600", color: colors.text, marginTop: 4 },
  muted: { color: colors.muted, fontSize: 14, textAlign: "center" },
  section: { gap: 12 },
  link: { color: colors.muted, textAlign: "center", textDecorationLine: "underline" },
});
