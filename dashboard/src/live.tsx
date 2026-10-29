import { type ReactNode, createContext, useContext, useEffect, useRef, useState } from "react";

import { type Notice, subscribeNotices } from "./api";

type Listener = (notice: Notice) => void;

interface Live {
  connected: boolean;
  listen: (listener: Listener) => () => void;
}

const LiveContext = createContext<Live | null>(null);

/** One notice stream per dashboard tab, shared by every view. */
export function LiveProvider({ children }: { children: ReactNode }) {
  const listeners = useRef(new Set<Listener>());
  const [connected, setConnected] = useState(false);

  useEffect(
    () =>
      subscribeNotices(
        (notice) => listeners.current.forEach((listener) => listener(notice)),
        setConnected,
      ),
    [],
  );

  const listen = (listener: Listener): (() => void) => {
    listeners.current.add(listener);
    return () => listeners.current.delete(listener);
  };
  return <LiveContext.Provider value={{ connected, listen }}>{children}</LiveContext.Provider>;
}

export function useLiveConnected(): boolean {
  return useContext(LiveContext)?.connected ?? false;
}

/** Call `refresh` (debounced) whenever a notice matches; notices only say what
 *  changed, so views re-read through the API (ADR-022). Also refreshes when the
 *  stream (re)connects, since notices may have been missed while it was down. */
export function useLiveRefresh(
  refresh: () => void,
  matches: (notice: Notice) => boolean,
): void {
  const live = useContext(LiveContext);
  const latest = useRef({ refresh, matches });
  latest.current = { refresh, matches };

  useEffect(() => {
    if (live === null) return undefined;
    let timer: number | undefined;
    const stop = live.listen((notice) => {
      if (notice.topic !== "hello" && !latest.current.matches(notice)) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => latest.current.refresh(), 250);
    });
    return () => {
      window.clearTimeout(timer);
      stop();
    };
  }, [live]);
}
