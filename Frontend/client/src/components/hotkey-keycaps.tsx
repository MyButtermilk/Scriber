import { Fragment } from "react";

/** One keycap per key; screen readers hear "Ctrl + Alt + X". */
export function HotkeyKeycaps({ keys }: { keys: string[] }) {
  return (
    <kbd className="hotkey-keycaps">
      {keys.map((key, index) => (
        <Fragment key={`${key}-${index}`}>
          {index > 0 ? <span className="sr-only"> + </span> : null}
          <kbd className="hotkey-keycap">{key}</kbd>
        </Fragment>
      ))}
    </kbd>
  );
}

/** A translated sentence whose {{hotkey}} placeholder renders as keycaps. */
export function HotkeySentence({ template, keys }: { template: string; keys: string[] }) {
  const [before, ...rest] = template.split("{{hotkey}}");
  if (rest.length === 0) return <>{template}</>;
  return (
    <>
      {before}
      <HotkeyKeycaps keys={keys} />
      {rest.join("")}
    </>
  );
}
