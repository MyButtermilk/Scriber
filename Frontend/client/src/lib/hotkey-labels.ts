type Translate = (source: string) => string;

/** Splits "Ctrl + Alt + X" or "ctrl+alt+x" into localized key labels. */
export function hotkeyKeyLabels(value: string | null | undefined, t: Translate): string[] {
  return String(value || "")
    .split("+")
    .map((part) => {
      const token = part.trim();
      const lower = token.toLowerCase();
      if (!lower) return "";
      if (lower === "ctrl" || lower === "control") return t("Ctrl");
      if (lower === "alt" || lower === "option") return "Alt";
      if (lower === "shift") return "Shift";
      if (["meta", "win", "windows", "cmd", "command", "super"].includes(lower)) return "Win";
      if (lower === "space") return t("Space");
      if (lower === "esc" || lower === "escape") return "Esc";
      if (token.length === 1) return token.toUpperCase();
      return token.charAt(0).toUpperCase() + token.slice(1);
    })
    .filter(Boolean);
}
