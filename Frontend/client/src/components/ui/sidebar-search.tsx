import { Search } from "lucide-react";
import { useEffect, useState } from "react";

interface SidebarSearchProps {
    placeholder?: string;
    onOpenCommandPalette?: () => void;
}

export function SidebarSearch({ placeholder = "Search", onOpenCommandPalette }: SidebarSearchProps) {
    const [isMac, setIsMac] = useState(false);

    useEffect(() => {
        setIsMac(navigator.platform.toLowerCase().includes('mac'));
    }, []);

    return (
        <button
            type="button"
            className="neu-search-inset flex w-full items-center gap-2 px-3 py-2.5 rounded-xl text-left cursor-pointer transition-all duration-200 hover:bg-accent/50"
            aria-label="Open command palette"
            onClick={() => onOpenCommandPalette?.()}
        >
            <Search className="w-4 h-4 text-muted-foreground shrink-0" />
            <span className="flex-1 min-w-0 text-sm text-muted-foreground">
                {placeholder}
            </span>
            <kbd className="neu-kbd shrink-0 inline-flex items-center px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground rounded">
                {isMac ? "⌘K" : "Strg+K"}
            </kbd>
        </button>
    );
}
