import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "hall-button inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-[var(--radius-input)] border text-sm font-medium leading-none outline-2 outline-offset-2 outline-transparent transition-[background-color,border-color,color,opacity,transform] duration-[var(--dur-micro)] ease-[var(--ease-out)] focus-visible:[outline-color:var(--color-focus)] active:translate-y-px disabled:cursor-not-allowed disabled:opacity-50 aria-disabled:cursor-not-allowed aria-disabled:opacity-50 aria-busy:cursor-wait aria-busy:opacity-75 motion-reduce:transform-none motion-reduce:transition-none [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "border-primary-border bg-primary text-primary-foreground",
        destructive: "border-destructive-border bg-destructive text-destructive-foreground",
        outline: "border-[var(--button-outline)] bg-background text-foreground",
        secondary: "border-secondary-border bg-secondary text-secondary-foreground",
        ghost: "border-transparent bg-transparent text-foreground",
        link: "border-transparent bg-transparent text-primary underline-offset-4",
      },
      size: {
        default: "h-11 px-4",
        sm: "h-9 px-3 text-xs",
        lg: "h-12 px-8",
        icon: "h-11 w-11",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        {...props}
        ref={ref}
        data-ui="button"
        data-variant={variant ?? "default"}
        data-size={size ?? "default"}
        className={cn(buttonVariants({ variant, size, className }))}
      />
    );
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
