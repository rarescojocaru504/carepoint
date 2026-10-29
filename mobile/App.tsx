import { NavigationContainer } from "@react-navigation/native";
import { createNativeStackNavigator } from "@react-navigation/native-stack";
import { StatusBar } from "expo-status-bar";
import { useEffect } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { Button, Centered, colors } from "./src/components/ui";
import { API_URL } from "./src/config";
import type { RootStackParamList } from "./src/navigation";
import { MachineScreen } from "./src/screens/MachineScreen";
import { MapScreen } from "./src/screens/MapScreen";
import { SignInScreen } from "./src/screens/SignInScreen";
import { useAuth } from "./src/store/auth";

const Stack = createNativeStackNavigator<RootStackParamList>();

export default function App() {
  const status = useAuth((s) => s.status);
  const restore = useAuth((s) => s.restore);

  useEffect(() => {
    void restore();
  }, [restore]);

  return (
    <SafeAreaProvider>
      <StatusBar style="dark" />
      {status === "restoring" ? (
        <Centered>
          <ActivityIndicator size="large" color={colors.primary} />
        </Centered>
      ) : status === "unreachable" ? (
        <Centered>
          <Text style={styles.title}>Can&apos;t reach CarePoint</Text>
          <Text style={styles.muted}>
            Check your connection. The app is talking to {API_URL}.
          </Text>
          <Button title="Try again" onPress={() => void restore()} />
        </Centered>
      ) : (
        <NavigationContainer>
          <Stack.Navigator screenOptions={{ headerTintColor: colors.primary }}>
            {status === "signedIn" ? (
              <>
                <Stack.Screen
                  name="Map"
                  component={MapScreen}
                  options={{ title: "Nearby", headerRight: () => <SignOutButton /> }}
                />
                <Stack.Screen
                  name="Machine"
                  component={MachineScreen}
                  options={({ route }) => ({ title: route.params.name })}
                />
              </>
            ) : (
              <Stack.Screen
                name="SignIn"
                component={SignInScreen}
                options={{ headerShown: false }}
              />
            )}
          </Stack.Navigator>
        </NavigationContainer>
      )}
    </SafeAreaProvider>
  );
}

function SignOutButton() {
  const signOut = useAuth((s) => s.signOut);
  return (
    <Pressable accessibilityRole="button" onPress={() => void signOut()} hitSlop={8}>
      <Text style={styles.headerButton}>Sign out</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  title: { fontSize: 20, fontWeight: "700", color: colors.text },
  muted: { color: colors.muted, textAlign: "center" },
  headerButton: { color: colors.primary, fontSize: 16 },
});
