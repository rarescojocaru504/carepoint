// Where the session tokens live (ADR-024).
//
// The 15-minute access token is kept in memory only. The rotating refresh
// token (ADR-009) is kept in expo-secure-store (the iOS Keychain / Android
// Keystore), so the app stays signed in across restarts without the token
// ever touching plain storage.

import * as SecureStore from "expo-secure-store";

const REFRESH_KEY = "carepoint.refresh_token";

let accessToken: string | null = null;

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export async function getRefreshToken(): Promise<string | null> {
  try {
    return await SecureStore.getItemAsync(REFRESH_KEY);
  } catch {
    return null; // keystore unavailable: behave as signed out
  }
}

export async function setRefreshToken(token: string): Promise<void> {
  await SecureStore.setItemAsync(REFRESH_KEY, token);
}

export async function clearTokens(): Promise<void> {
  accessToken = null;
  try {
    await SecureStore.deleteItemAsync(REFRESH_KEY);
  } catch {
    // nothing stored
  }
}
