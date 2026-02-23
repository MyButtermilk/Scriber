import { Monitor } from "lucide-react";
import { cn } from "@/lib/utils";
import {
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuItem,
    DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useTheme } from "@/components/theme-provider";

type ThemeToggleProps = {
    align?: "compact" | "edge";
};

export function ThemeToggle({ align = "compact" }: ThemeToggleProps) {
    const { theme, resolvedTheme, setTheme } = useTheme();
    const isDark = resolvedTheme === "dark";

    return (
        <div className={cn("flex items-center gap-1", align === "edge" && "w-full px-1.5")}>
            <button
                type="button"
                role="switch"
                aria-checked={isDark}
                aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
                title={isDark ? "Dark mode active" : "Light mode active"}
                className={`magic-theme-toggle ${isDark ? "is-dark" : ""}`}
                onClick={() => setTheme(isDark ? "light" : "dark")}
            >
                <span className="sr-only">
                    {isDark ? "Dark mode is active" : "Light mode is active"}
                </span>

                <span className="magic-theme-toggle__clouds" aria-hidden="true">
                    <svg className="magic-theme-toggle__cloud magic-theme-toggle__cloud--1" viewBox="0 0 24 24">
                        <path d="M17.5 19C19.9853 19 22 16.9853 22 14.5C22 12.1325 20.1764 10.1906 17.8596 10.0223C17.4116 7.18945 14.9458 5 12 5C9.44521 5 7.28821 6.64333 6.38605 8.91494C6.26257 8.90515 6.13313 8.90002 6 8.90002C3.23858 8.90002 1 11.1386 1 13.9C1 16.6614 3.23858 19 6 19H17.5Z" />
                    </svg>
                    <svg className="magic-theme-toggle__cloud magic-theme-toggle__cloud--2" viewBox="0 0 24 24">
                        <path d="M17.5 19C19.9853 19 22 16.9853 22 14.5C22 12.1325 20.1764 10.1906 17.8596 10.0223C17.4116 7.18945 14.9458 5 12 5C9.44521 5 7.28821 6.64333 6.38605 8.91494C6.26257 8.90515 6.13313 8.90002 6 8.90002C3.23858 8.90002 1 11.1386 1 13.9C1 16.6614 3.23858 19 6 19H17.5Z" />
                    </svg>
                    <svg className="magic-theme-toggle__cloud magic-theme-toggle__cloud--3" viewBox="0 0 24 24">
                        <path d="M17.5 19C19.9853 19 22 16.9853 22 14.5C22 12.1325 20.1764 10.1906 17.8596 10.0223C17.4116 7.18945 14.9458 5 12 5C9.44521 5 7.28821 6.64333 6.38605 8.91494C6.26257 8.90515 6.13313 8.90002 6 8.90002C3.23858 8.90002 1 11.1386 1 13.9C1 16.6614 3.23858 19 6 19H17.5Z" />
                    </svg>
                </span>

                <span className="magic-theme-toggle__stars" aria-hidden="true">
                    <svg className="magic-theme-toggle__star magic-theme-toggle__star--1" viewBox="0 0 24 24"><path d="M12 2L14.4 9.6H22.4L16 14.4L18.4 22L12 17.2L5.6 22L8 14.4L1.6 9.6H9.6L12 2Z" /></svg>
                    <svg className="magic-theme-toggle__star magic-theme-toggle__star--2" viewBox="0 0 24 24"><path d="M12 2L14.4 9.6H22.4L16 14.4L18.4 22L12 17.2L5.6 22L8 14.4L1.6 9.6H9.6L12 2Z" /></svg>
                    <svg className="magic-theme-toggle__star magic-theme-toggle__star--3" viewBox="0 0 24 24"><path d="M12 2L14.4 9.6H22.4L16 14.4L18.4 22L12 17.2L5.6 22L8 14.4L1.6 9.6H9.6L12 2Z" /></svg>
                    <svg className="magic-theme-toggle__star magic-theme-toggle__star--4" viewBox="0 0 24 24"><path d="M12 2L14.4 9.6H22.4L16 14.4L18.4 22L12 17.2L5.6 22L8 14.4L1.6 9.6H9.6L12 2Z" /></svg>
                    <svg className="magic-theme-toggle__star magic-theme-toggle__star--5" viewBox="0 0 24 24"><path d="M12 2L14.4 9.6H22.4L16 14.4L18.4 22L12 17.2L5.6 22L8 14.4L1.6 9.6H9.6L12 2Z" /></svg>
                    <svg className="magic-theme-toggle__star magic-theme-toggle__star--6" viewBox="0 0 24 24"><path d="M12 2L14.4 9.6H22.4L16 14.4L18.4 22L12 17.2L5.6 22L8 14.4L1.6 9.6H9.6L12 2Z" /></svg>
                </span>

                <span className="magic-theme-toggle__thumb" aria-hidden="true">
                    <span className="magic-theme-toggle__sun">
                        <svg
                            width="16"
                            height="16"
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="rgba(255,255,255,0.9)"
                            strokeWidth="2.2"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                        >
                            <circle cx="12" cy="12" r="5" fill="rgba(255,255,255,0.85)" />
                            <line x1="12" y1="1" x2="12" y2="3" />
                            <line x1="12" y1="21" x2="12" y2="23" />
                            <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
                            <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
                            <line x1="1" y1="12" x2="3" y2="12" />
                            <line x1="21" y1="12" x2="23" y2="12" />
                            <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
                            <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
                        </svg>
                    </span>
                    <span className="magic-theme-toggle__moon">
                        <span className="magic-theme-toggle__crater magic-theme-toggle__crater--1" />
                        <span className="magic-theme-toggle__crater magic-theme-toggle__crater--2" />
                        <span className="magic-theme-toggle__crater magic-theme-toggle__crater--3" />
                        <span className="magic-theme-toggle__crater magic-theme-toggle__crater--4" />
                    </span>
                </span>
            </button>

            <DropdownMenu>
                <DropdownMenuTrigger asChild>
                    <button
                        type="button"
                        aria-label="Theme mode options"
                        className={cn(
                            "h-7 w-7 shrink-0 rounded-full border border-border/60 bg-card/70 text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
                            theme === "system" && (align === "edge" ? "text-primary" : "border-primary/60 text-primary"),
                            align === "edge" && "ml-auto mr-0.5 rounded-md border-transparent bg-transparent text-muted-foreground/80 hover:bg-accent/45",
                        )}
                    >
                        <Monitor className="mx-auto h-3.5 w-3.5" />
                    </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                    <DropdownMenuItem onClick={() => setTheme("light")} className="justify-between gap-3">
                        <span>Light</span>
                        {theme === "light" && <span className="text-primary">✓</span>}
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => setTheme("dark")} className="justify-between gap-3">
                        <span>Dark</span>
                        {theme === "dark" && <span className="text-primary">✓</span>}
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => setTheme("system")} className="justify-between gap-3">
                        <span>System</span>
                        {theme === "system" && <span className="text-primary">✓</span>}
                    </DropdownMenuItem>
                </DropdownMenuContent>
            </DropdownMenu>
        </div>
    );
}
