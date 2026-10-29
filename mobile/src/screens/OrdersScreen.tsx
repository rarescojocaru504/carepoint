import { useFocusEffect } from "@react-navigation/native";
import { useCallback } from "react";
import { Pressable, RefreshControl, SectionList, StyleSheet, Text, View } from "react-native";

import { Banner, colors } from "../components/ui";
import { formatPrice } from "../format";
import { friendlyError } from "../messages";
import type { ScreenProps } from "../navigation";
import { isActive, useReservations } from "../store/reservations";
import type { Order, Reservation } from "../store/reservations";

const STATUS_LABEL: Record<Reservation["status"], string> = {
  held: "Held, not paid yet",
  paid: "Paid, ready to pick up",
  dispensed: "Picked up",
  expired: "Expired",
  cancelled: "Cancelled",
};

const PAYMENT_LABEL: Record<Order["payment_status"], string> = {
  authorized: "Card authorised",
  captured: "Charged",
  voided: "Not charged",
  failed: "Payment failed",
};

type Row = { kind: "active"; reservation: Reservation } | { kind: "order"; order: Order };

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function OrdersScreen({ navigation }: ScreenProps<"Orders">) {
  const mine = useReservations((s) => s.mine);
  const orders = useReservations((s) => s.orders);
  const loadMine = useReservations((s) => s.loadMine);
  const loadOrders = useReservations((s) => s.loadOrders);

  const refresh = useCallback(() => {
    void loadMine();
    void loadOrders();
  }, [loadMine, loadOrders]);
  useFocusEffect(refresh);

  const active: Row[] = (mine.data ?? [])
    .filter(isActive)
    .map((reservation) => ({ kind: "active", reservation }));
  const history: Row[] = (orders.data ?? []).map((order) => ({ kind: "order", order }));
  const sections = [
    ...(active.length > 0 ? [{ title: "Active", data: active }] : []),
    { title: "Order history", data: history },
  ];
  const failure =
    mine.status === "error" ? mine.error : orders.status === "error" ? orders.error : null;
  const open = (reservationId: string) => navigation.navigate("Reservation", { reservationId });

  return (
    <SectionList
      style={styles.screen}
      sections={sections}
      keyExtractor={(row) => (row.kind === "active" ? row.reservation.id : row.order.id)}
      refreshControl={
        <RefreshControl
          refreshing={mine.status === "loading" || orders.status === "loading"}
          onRefresh={refresh}
        />
      }
      ListHeaderComponent={
        failure !== null ? (
          <View style={styles.notice}>
            <Banner tone="error">{friendlyError(failure)}</Banner>
          </View>
        ) : undefined
      }
      renderSectionHeader={({ section }) => <Text style={styles.heading}>{section.title}</Text>}
      renderSectionFooter={({ section }) =>
        section.data.length === 0 && orders.status === "ready" ? (
          <Text style={styles.empty}>No orders yet.</Text>
        ) : null
      }
      renderItem={({ item }) =>
        item.kind === "active" ? (
          <OrderRow
            title={item.reservation.product_name}
            detail={STATUS_LABEL[item.reservation.status]}
            amount={formatPrice(item.reservation.total_cents, item.reservation.currency)}
            when={formatDate(item.reservation.created_at)}
            onPress={() => open(item.reservation.id)}
          />
        ) : (
          <OrderRow
            title={item.order.product_name}
            detail={`${STATUS_LABEL[item.order.reservation_status]} · ${PAYMENT_LABEL[item.order.payment_status]}`}
            amount={formatPrice(item.order.amount_cents, item.order.currency)}
            when={formatDate(item.order.created_at)}
            onPress={() => open(item.order.reservation_id)}
          />
        )
      }
    />
  );
}

function OrderRow(props: {
  title: string;
  detail: string;
  amount: string;
  when: string;
  onPress: () => void;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      onPress={props.onPress}
      style={({ pressed }) => [styles.row, pressed && styles.pressed]}
    >
      <View style={styles.rowMain}>
        <Text style={styles.title}>{props.title}</Text>
        <Text style={styles.muted}>{props.detail}</Text>
        <Text style={styles.muted}>{props.when}</Text>
      </View>
      <Text style={styles.amount}>{props.amount}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background },
  notice: { padding: 12 },
  heading: {
    paddingHorizontal: 16,
    paddingTop: 20,
    paddingBottom: 8,
    fontSize: 13,
    fontWeight: "700",
    color: colors.muted,
    textTransform: "uppercase",
  },
  empty: { paddingHorizontal: 16, color: colors.muted },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    padding: 16,
    backgroundColor: "#fff",
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.border,
  },
  pressed: { backgroundColor: colors.background },
  rowMain: { flex: 1, gap: 2 },
  title: { fontSize: 16, fontWeight: "600", color: colors.text },
  muted: { color: colors.muted, fontSize: 14 },
  amount: { fontSize: 15, fontWeight: "600", color: colors.text },
});
