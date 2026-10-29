import { useState } from "react";
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { Banner, Button, colors } from "../components/ui";
import { errorMessage } from "../services/api";
import { useAuth } from "../store/auth";

const MIN_PASSWORD = 12; // the backend's rule for new accounts (ADR-009)

export function SignInScreen() {
  const signIn = useAuth((s) => s.signIn);
  const register = useAuth((s) => s.register);
  const [mode, setMode] = useState<"signIn" | "register">("signIn");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const registering = mode === "register";
  const problem =
    email.trim() === "" || password === ""
      ? "Enter your email and password."
      : registering && fullName.trim() === ""
        ? "Enter your name."
        : registering && password.length < MIN_PASSWORD
          ? `Use a password of at least ${MIN_PASSWORD} characters.`
          : null;

  const submit = async () => {
    if (problem !== null) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (registering) await register(fullName.trim(), email.trim(), password);
      else await signIn(email.trim(), password);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  };

  return (
    <KeyboardAvoidingView
      style={styles.screen}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Text style={styles.title}>CarePoint</Text>
        <Text style={styles.subtitle}>Urgent care items, reserved before you get there.</Text>

        {registering && (
          <TextInput
            style={styles.input}
            placeholder="Full name"
            accessibilityLabel="Full name"
            autoComplete="name"
            value={fullName}
            onChangeText={setFullName}
          />
        )}
        <TextInput
          style={styles.input}
          placeholder="Email"
          accessibilityLabel="Email"
          autoCapitalize="none"
          autoComplete="email"
          keyboardType="email-address"
          value={email}
          onChangeText={setEmail}
        />
        <TextInput
          style={styles.input}
          placeholder="Password"
          accessibilityLabel="Password"
          autoComplete={registering ? "new-password" : "current-password"}
          secureTextEntry
          value={password}
          onChangeText={setPassword}
          onSubmitEditing={() => void submit()}
        />

        {error !== null && <Banner tone="error">{error}</Banner>}

        <Button
          title={registering ? "Create account" : "Sign in"}
          busy={busy}
          onPress={() => void submit()}
        />

        <Pressable
          accessibilityRole="button"
          onPress={() => {
            setMode(registering ? "signIn" : "register");
            setError(null);
          }}
        >
          <Text style={styles.switch}>
            {registering ? "I already have an account" : "New here? Create an account"}
          </Text>
        </Pressable>

        {!registering && (
          <View style={styles.hint}>
            <Text style={styles.hintText}>
              Demo account: customer@carepoint.demo / carepoint-customer-demo
            </Text>
          </View>
        )}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: "#fff" },
  content: { flexGrow: 1, justifyContent: "center", padding: 24, gap: 12 },
  title: { fontSize: 32, fontWeight: "700", color: colors.primary },
  subtitle: { fontSize: 15, color: colors.muted, marginBottom: 16 },
  input: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 10,
    paddingHorizontal: 14,
    paddingVertical: 12,
    fontSize: 16,
    color: colors.text,
  },
  switch: { color: colors.primary, textAlign: "center", paddingVertical: 8, fontWeight: "500" },
  hint: { marginTop: 8 },
  hintText: { color: colors.muted, fontSize: 13, textAlign: "center" },
});
