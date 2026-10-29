// react-native-maps is native; render the map and markers as plain views.
jest.mock("react-native-maps", () => {
  const { View } = jest.requireActual<typeof import("react-native")>("react-native");
  const MapView = (props: { children?: import("react").ReactNode }) => (
    <View testID="map">{props.children}</View>
  );
  const Marker = (props: { title?: string }) => <View testID={`marker:${props.title ?? ""}`} />;
  return { __esModule: true, default: MapView, Marker };
});

// The QR code draws with react-native-svg; tests only need to know what it encodes.
jest.mock("react-native-qrcode-svg", () => {
  const { View } = jest.requireActual<typeof import("react-native")>("react-native");
  const QRCode = (props: { value: string }) => <View testID={`qr:${props.value}`} />;
  return { __esModule: true, default: QRCode };
});

// Without this, SafeAreaProvider renders nothing until it has measured a screen.
jest.mock(
  "react-native-safe-area-context",
  () =>
    jest.requireActual<{ default: object }>("react-native-safe-area-context/jest/mock").default,
);

// expo-secure-store is native; tests use an in-memory stand-in.
jest.mock("expo-secure-store", () => {
  const store = new Map<string, string>();
  return {
    getItemAsync: jest.fn((key: string) => Promise.resolve(store.get(key) ?? null)),
    setItemAsync: jest.fn((key: string, value: string) => {
      store.set(key, value);
      return Promise.resolve();
    }),
    deleteItemAsync: jest.fn((key: string) => {
      store.delete(key);
      return Promise.resolve();
    }),
    __reset: () => store.clear(),
  };
});
