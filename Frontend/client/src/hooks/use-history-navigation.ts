import { useEffect } from "react";

// Route links already use pushState. Explicit History API traversal also visits
// entries which a WebView's browser-level back action may skip.
export function useHistoryNavigation() {
  useEffect(() => {
    const sideButton = (event: MouseEvent) => event.button === 3 || event.button === 4;
    const suppressNative = (event: MouseEvent) => {
      if (!sideButton(event)) return;
      event.preventDefault();
      event.stopPropagation();
    };
    const mouseUp = (event: MouseEvent) => {
      if (!sideButton(event)) return;
      suppressNative(event);
      window.history.go(event.button === 3 ? -1 : 1);
    };
    const keyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.ctrlKey || event.metaKey || event.shiftKey) return;
      const back = event.key === "BrowserBack" || (event.altKey && event.key === "ArrowLeft");
      const forward = event.key === "BrowserForward" || (event.altKey && event.key === "ArrowRight");
      if (!back && !forward) return;
      event.preventDefault();
      window.history.go(back ? -1 : 1);
    };
    window.addEventListener("mousedown", suppressNative, true);
    window.addEventListener("mouseup", mouseUp, true);
    window.addEventListener("auxclick", suppressNative, true);
    window.addEventListener("keydown", keyDown);
    return () => {
      window.removeEventListener("mousedown", suppressNative, true);
      window.removeEventListener("mouseup", mouseUp, true);
      window.removeEventListener("auxclick", suppressNative, true);
      window.removeEventListener("keydown", keyDown);
    };
  }, []);
}
