import { useEffect } from "react";
import { AppState, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useAuth } from "../store/auth";
import { useLive } from "../store/live";
import { colors } from "./ui";

/** Keep the live connection open while signed in and in the foreground.
 * Coming back to the foreground reconnects, and the server's "hello" makes
 * the app re-read what it shows, since notices were missed meanwhile. */
export function useLiveUpdates(): void {
  const signedIn = useAuth((s) => s.status === "signedIn");
  const start = useLive((s) => s.start);
  const stop = useLive((s) => s.stop);

  useEffect(() => {
    if (!signedIn) {
      stop();
      return;
    }
    if (AppState.currentState !== "background") start();
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active") start();
      else if (state === "background") stop();
    });
    return () => {
      subscription.remove();
      stop();
    };
  }, [signedIn, start, stop]);
}

/** Shown at the bottom of every screen while live updates are interrupted. */
export function ConnectionBanner() {
  const reconnecting = useLive((s) => s.status === "reconnecting");
  const insets = useSafeAreaInsets();
  if (!reconnecting) return null;
  return (
    <View
      accessibilityRole="alert"
      accessibilityLiveRegion="polite"
      style={[styles.banner, { paddingBottom: 10 + insets.bottom }]}
    >
      <Text style={styles.text}>Reconnecting… live updates are paused.</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  banner: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    paddingTop: 10,
    paddingHorizontal: 16,
    backgroundColor: colors.text,
  },
  text: { color: "#fff", textAlign: "center", fontSize: 14 },
});
