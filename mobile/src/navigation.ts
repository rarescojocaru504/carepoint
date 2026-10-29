import type { NativeStackScreenProps } from "@react-navigation/native-stack";

/** Screens and their parameters. Signed-out users only ever see SignIn. */
export type RootStackParamList = {
  SignIn: undefined;
  Map: undefined;
  Machine: { machineId: string; name: string };
};

export type ScreenProps<Name extends keyof RootStackParamList> = NativeStackScreenProps<
  RootStackParamList,
  Name
>;
