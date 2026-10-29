import { StatusBar } from "expo-status-bar";
import { useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { API_URL } from "./src/config";
import { api, errorMessage, unwrap } from "./src/services/api";
import { useAuth } from "./src/store/auth";

type Check = { state: "checking" } | { state: "ok" } | { state: "failed"; message: string };

async function checkHealth(): Promise<Check> {
  try {
    await unwrap(api.GET("/health"));
    return { state: "ok" };
  } catch (error) {
    return { state: "failed", message: errorMessage(error) };
  }
}

// Phase 9-B placeholder: shows that the app reaches the API and restores a
// stored session. Sign-in, the map and machine screens arrive in 9-C.
export default function App() {
  const status = useAuth((s) => s.status);
  const user = useAuth((s) => s.user);
  const restore = useAuth((s) => s.restore);
  const [check, setCheck] = useState<Check>({ state: "checking" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    void checkHealth()
      .then((result) => {
        if (active) setCheck(result);
      })
      .then(restore);
    return () => {
      active = false;
    };
  }, [attempt, restore]);

  const run = () => {
    setCheck({ state: "checking" });
    setAttempt((n) => n + 1);
  };

  return (
    <View style={styles.container}>
      <Text style={styles.title}>CarePoint</Text>
      <Text style={styles.line}>API: {API_URL}</Text>
      <Text style={styles.line}>
        {check.state === "checking" && "Checking the connection…"}
        {check.state === "ok" && "Connected ✓"}
        {check.state === "failed" && `Not connected: ${check.message}`}
      </Text>
      <Text style={styles.line}>
        Session: {status}
        {user !== null ? ` (${user.email})` : ""}
      </Text>
      <Pressable style={styles.button} onPress={run} accessibilityRole="button">
        <Text style={styles.buttonText}>Check again</Text>
      </Pressable>
      <StatusBar style="auto" />
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: "#fff",
    alignItems: "center",
    justifyContent: "center",
    padding: 24,
    gap: 8,
  },
  title: { fontSize: 28, fontWeight: "700", marginBottom: 8 },
  line: { fontSize: 15, textAlign: "center" },
  button: {
    marginTop: 16,
    backgroundColor: "#0f766e",
    paddingHorizontal: 20,
    paddingVertical: 12,
    borderRadius: 8,
  },
  buttonText: { color: "#fff", fontWeight: "600" },
});
