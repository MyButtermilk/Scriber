import * as React from "react";

import { cn } from "@/lib/utils";

const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        {...props}
        type={type}
        ref={ref}
        data-ui="input"
        className={cn(
          "hall-input flex h-11 w-full rounded-[var(--radius-input)] border border-input bg-background px-3 py-2 text-base text-foreground outline-2 outline-offset-2 outline-transparent transition-[background-color,border-color,color,opacity] duration-[var(--dur-short)] ease-[var(--ease-out)] file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:border-ring focus-visible:[outline-color:var(--color-focus)] aria-invalid:border-destructive aria-busy:cursor-wait disabled:cursor-not-allowed disabled:opacity-50 motion-reduce:transition-none md:text-sm",
          className,
        )}
      />
    );
  },
);
Input.displayName = "Input";

export { Input };
