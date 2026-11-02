// Where to search from: the phone's position if the customer allows it,
// otherwise the Cluj-Napoca demo area where the seeded machines are (ADR-025, ADR-033).

import * as Location from "expo-location";

export interface Point {
  lat: number;
  lng: number;
}

export type PointSource = "device" | "demo";

export const DEMO_CENTER: Point = { lat: 46.77, lng: 23.59 };

export async function startPoint(): Promise<{ point: Point; source: PointSource }> {
  try {
    const permission = await Location.requestForegroundPermissionsAsync();
    if (permission.status !== Location.PermissionStatus.GRANTED) {
      return { point: DEMO_CENTER, source: "demo" };
    }
    const position =
      (await Location.getLastKnownPositionAsync({ maxAge: 5 * 60_000 })) ??
      (await Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.Balanced }));
    return {
      point: { lat: position.coords.latitude, lng: position.coords.longitude },
      source: "device",
    };
  } catch {
    return { point: DEMO_CENTER, source: "demo" }; // location services off
  }
}
