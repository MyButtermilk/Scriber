import { Search } from "lucide-react";
import { useEffect, useRef, useState } from "react";

interface SidebarSearchProps {
    placeholder?: string;
    onSearch?: (query: string) => void;
}

export function SidebarSearch({ placeholder = "Search", onSearch }: SidebarSearchProps) {
    const inputRef = useRef<HTMLInputElement>(null);
    const [isMac, setIsMac] = useState(false);

    useEffect(() => {
        setIsMac(navigator.platform.toLowerCase().includes('mac'));
    }, []);

    useEffect(() => {
        const handleKeyDown = (e: KeyboardEvent) => {
            if ((e.ctrlKey || e.metaKey) && e.key === "k") {
                e.preventDefault();
                inputRef.current?.focus();
            }
        };

        document.addEventListener("keydown", handleKeyDown);
        return () => document.removeEventListener("keydown", handleKeyDown);
    }, []);

    return (
        <div
            className="neu-search-inset flex items-center gap-2 px-3 py-2.5 rounded-xl cursor-text transition-all duration-200"
            onClick={() => inputRef.current?.focus()}
        >
            <Search className="w-4 h-4 text-muted-foreground shrink-0" />
            <input
                ref={inputRef}
                type="text"
                placeholder={placeholder}
                onChange={(e) => onSearch?.(e.target.value)}
                className="flex-1 min-w-0 bg-transparent text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
            />
            <kbd className="neu-kbd shrink-0 inline-flex items-center px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground rounded">
                {isMac ? "⌘K" : "STRG+K"}
            </kbd>
        </div>
    );
}
