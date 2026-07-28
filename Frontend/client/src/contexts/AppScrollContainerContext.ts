import { createContext, useContext, type RefObject } from "react";

export const AppScrollContainerContext = createContext<RefObject<HTMLDivElement | null> | null>(null);

export function useAppScrollContainerRef(): RefObject<HTMLDivElement | null> {
  const context = useContext(AppScrollContainerContext);
  if (!context) {
    throw new Error("useAppScrollContainerRef must be used within AppLayout");
  }
  return context;
}
