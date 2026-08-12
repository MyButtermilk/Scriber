"use client";

import * as React from "react";
import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown, ChevronUp } from "lucide-react";

import { cn } from "@/lib/utils";

const Select = SelectPrimitive.Root;

const SelectGroup = SelectPrimitive.Group;

const SelectValue = SelectPrimitive.Value;

const SelectTrigger = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Trigger>
>(({ className, children, ...props }, ref) => (
  <SelectPrimitive.Trigger
    {...props}
    ref={ref}
    data-ui="select-trigger"
    className={cn(
      "hall-select__trigger group/select flex h-11 w-full items-center justify-between gap-2 whitespace-nowrap rounded-[var(--radius-input)] border border-input bg-background px-3 py-2 text-sm font-medium text-foreground outline-2 outline-offset-2 outline-transparent transition-[background-color,border-color,color,opacity,transform] duration-[var(--dur-short)] ease-[var(--ease-out)] data-[placeholder]:text-muted-foreground focus-visible:border-ring focus-visible:[outline-color:var(--color-focus)] active:translate-y-px data-[state=open]:translate-y-0 data-[state=open]:border-ring data-[state=open]:bg-background disabled:cursor-not-allowed disabled:opacity-50 motion-reduce:transform-none motion-reduce:transition-none [&>span]:line-clamp-1",
      className,
    )}
  >
    {children}
    <SelectPrimitive.Icon asChild>
      <ChevronDown className="hall-select__icon h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-[var(--dur-short)] ease-[var(--ease-out)] group-data-[state=open]/select:rotate-180 motion-reduce:transform-none motion-reduce:transition-none" />
    </SelectPrimitive.Icon>
  </SelectPrimitive.Trigger>
));
SelectTrigger.displayName = SelectPrimitive.Trigger.displayName;

const SelectScrollUpButton = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.ScrollUpButton>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.ScrollUpButton>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.ScrollUpButton
    {...props}
    ref={ref}
    data-ui="select-scroll-up"
    className={cn("hall-select__scroll-button flex h-8 cursor-default items-center justify-center", className)}
  >
    <ChevronUp className="h-4 w-4" />
  </SelectPrimitive.ScrollUpButton>
));
SelectScrollUpButton.displayName = SelectPrimitive.ScrollUpButton.displayName;

const SelectScrollDownButton = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.ScrollDownButton>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.ScrollDownButton>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.ScrollDownButton
    {...props}
    ref={ref}
    data-ui="select-scroll-down"
    className={cn("hall-select__scroll-button flex h-8 cursor-default items-center justify-center", className)}
  >
    <ChevronDown className="h-4 w-4" />
  </SelectPrimitive.ScrollDownButton>
));
SelectScrollDownButton.displayName = SelectPrimitive.ScrollDownButton.displayName;

const SelectContent = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Content>
>(({ className, children, position = "popper", ...props }, ref) => (
  <SelectPrimitive.Portal>
    <SelectPrimitive.Content
      {...props}
      ref={ref}
      data-ui="select-content"
      className={cn(
        "hall-select__content relative z-[var(--z-dropdown)] max-h-[var(--radix-select-content-available-height)] min-w-[8rem] origin-[var(--radix-select-content-transform-origin)] overflow-y-auto overflow-x-hidden rounded-[var(--radius-card)] border border-border bg-popover text-popover-foreground shadow-[var(--shadow-popover)] ease-[var(--ease-out)] data-[state=open]:animate-in data-[state=open]:duration-[var(--dur-short)] data-[state=open]:fade-in-0 data-[state=open]:zoom-in-[0.98] data-[state=closed]:animate-out data-[state=closed]:duration-[var(--dur-micro)] data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-[0.99] data-[state=closed]:ease-[var(--ease-in)] motion-reduce:[--tw-enter-scale:1] motion-reduce:[--tw-exit-scale:1]",
        position === "popper" &&
          "data-[side=bottom]:translate-y-1 data-[side=left]:-translate-x-1 data-[side=right]:translate-x-1 data-[side=top]:-translate-y-1",
        className,
      )}
      position={position}
    >
      <SelectScrollUpButton />
      <SelectPrimitive.Viewport
        data-ui="select-viewport"
        className={cn(
          "hall-select__viewport p-1.5",
          position === "popper" &&
            "h-[var(--radix-select-trigger-height)] w-full min-w-[var(--radix-select-trigger-width)]",
        )}
      >
        {children}
      </SelectPrimitive.Viewport>
      <SelectScrollDownButton />
    </SelectPrimitive.Content>
  </SelectPrimitive.Portal>
));
SelectContent.displayName = SelectPrimitive.Content.displayName;

const SelectLabel = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Label>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Label>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.Label
    {...props}
    ref={ref}
    data-ui="select-label"
    className={cn("hall-select__label px-2 py-1.5 text-sm font-semibold", className)}
  />
));
SelectLabel.displayName = SelectPrimitive.Label.displayName;

const SelectItem = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Item>
>(({ className, children, ...props }, ref) => (
  <SelectPrimitive.Item
    {...props}
    ref={ref}
    data-ui="select-item"
    className={cn(
      "hall-select__item relative flex min-h-11 w-full cursor-default select-none items-center rounded-[var(--radius-input)] py-2 pl-3 pr-10 text-sm outline-none transition-[background-color,color] duration-[var(--dur-micro)] ease-[var(--ease-out)] focus:bg-accent focus:text-accent-foreground data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground data-[state=checked]:bg-primary/[0.08] data-[state=checked]:text-foreground data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50 motion-reduce:transition-none",
      className,
    )}
  >
    <span className="hall-select__indicator absolute right-2.5 flex h-5 w-5 items-center justify-center rounded-[var(--radius-input)] text-primary">
      <SelectPrimitive.ItemIndicator>
        <Check className="h-3.5 w-3.5" strokeWidth={2.4} />
      </SelectPrimitive.ItemIndicator>
    </span>
    <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
  </SelectPrimitive.Item>
));
SelectItem.displayName = SelectPrimitive.Item.displayName;

const SelectSeparator = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Separator>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Separator>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.Separator
    {...props}
    ref={ref}
    data-ui="select-separator"
    className={cn("hall-select__separator -mx-1 my-1 h-px bg-border", className)}
  />
));
SelectSeparator.displayName = SelectPrimitive.Separator.displayName;

export {
  Select,
  SelectGroup,
  SelectValue,
  SelectTrigger,
  SelectContent,
  SelectLabel,
  SelectItem,
  SelectSeparator,
  SelectScrollUpButton,
  SelectScrollDownButton,
};
