// Where the CarePoint API lives. A phone running Expo Go can't reach the
// computer's "localhost", so start Expo with your computer's LAN address:
//   EXPO_PUBLIC_API_URL=http://192.168.1.23:8000 npx expo start
// The Android emulator reaches the host computer at http://10.0.2.2:8000.

const DEFAULT_API_URL = "http://localhost:8000";

export function apiBaseUrl(value: string | undefined = process.env.EXPO_PUBLIC_API_URL): string {
  const url = (value ?? "").trim();
  return (url === "" ? DEFAULT_API_URL : url).replace(/\/+$/, "");
}

export const API_URL = apiBaseUrl();
