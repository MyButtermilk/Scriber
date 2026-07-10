import { Search, X } from "lucide-react";

import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

interface TranscriptHistorySearchProps {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  ariaLabel: string;
  clearLabel: string;
  className?: string;
}

export function TranscriptHistorySearch({
  value,
  onChange,
  placeholder,
  ariaLabel,
  clearLabel,
  className,
}: TranscriptHistorySearchProps) {
  return (
    <div
      className={cn(
        "transcript-history-search neu-search-inset group relative min-w-0 flex-1 sm:flex-none",
        className,
      )}
    >
      <Search
        className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground transition-colors group-focus-within:text-foreground"
        aria-hidden="true"
      />
      <Input
        type="search"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        aria-label={ariaLabel}
        className="h-11 rounded-[inherit] border-0 bg-transparent pl-10 pr-10 text-sm shadow-none outline-none focus-visible:ring-0"
      />
      {value ? (
        <button
          type="button"
          onClick={() => onChange("")}
          className="absolute right-2 top-1/2 inline-flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-[10px] text-muted-foreground outline-none transition-[color,background-color,transform] hover:bg-foreground/[0.06] hover:text-foreground active:scale-95 focus-visible:ring-2 focus-visible:ring-ring/60"
          aria-label={clearLabel}
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      ) : null}
    </div>
  );
}
