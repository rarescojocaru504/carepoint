// react-native-maps is native; render the map and markers as plain views.
jest.mock("react-native-maps", () => {
  const { View } = jest.requireActual<typeof import("react-native")>("react-native");
  const MapView = (props: { children?: import("react").ReactNode }) => <View testID="map">{props.children}</View>;
  const Marker = (props: { title?: string }) => <View testID={`marker:${props.title ?? ""}`} />;
  return { __esModule: true, default: MapView, Marker };
});

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
