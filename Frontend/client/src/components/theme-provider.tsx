import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { isTauriRuntime } from "@/lib/backend";

type Theme = "dark" | "light" | "system";
type ResolvedTheme = "dark" | "light";

type ThemeTransitionOptions = {
    origin?: {
        x: number;
        y: number;
    };
};

type ThemeProviderProps = {
    children: React.ReactNode;
    defaultTheme?: Theme;
    storageKey?: string;
};

type ThemeProviderState = {
    theme: Theme;
    setTheme: (theme: Theme, options?: ThemeTransitionOptions) => void;
    resolvedTheme: ResolvedTheme;
};

const initialState: ThemeProviderState = {
    theme: "system",
    setTheme: () => null,
    resolvedTheme: "dark",
};

const ThemeProviderContext = createContext<ThemeProviderState>(initialState);
const THEME_TRANSITION_DURATION_MS = 760;
const THEME_REVEAL_OVERLAY_CLASS = "theme-reveal-overlay";
const THEME_REVEAL_ACTIVE_DATASET_KEY = "themeRevealActive";

type ViewTransition = {
    ready: Promise<void>;
    finished: Promise<void>;
};

type DocumentWithViewTransition = Document & {
    startViewTransition?: (updateCallback: () => void) => ViewTransition;
};

function resolveEffectiveTheme(theme: Theme): ResolvedTheme {
    if (theme === "system") {
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    return theme;
}

function applyThemeClass(theme: ResolvedTheme) {
    const root = window.document.documentElement;
    root.classList.remove("light", "dark");
    root.classList.add(theme);
    root.style.colorScheme = theme;
}

function setThemeRevealActive(active: boolean) {
    const root = window.document.documentElement;
    if (active) {
        root.dataset[THEME_REVEAL_ACTIVE_DATASET_KEY] = "true";
        return;
    }
    delete root.dataset[THEME_REVEAL_ACTIVE_DATASET_KEY];
}

function circularThemeReveal(origin: { x: number; y: number }, transition: ViewTransition) {
    const endRadius = Math.hypot(
        Math.max(origin.x, window.innerWidth - origin.x),
        Math.max(origin.y, window.innerHeight - origin.y),
    );
    const clipPath = [
        `circle(0px at ${origin.x}px ${origin.y}px)`,
        `circle(${endRadius}px at ${origin.x}px ${origin.y}px)`,
    ];

    void transition.ready.then(() => {
        window.document.documentElement.animate(
            { clipPath },
            {
                duration: THEME_TRANSITION_DURATION_MS,
                easing: "cubic-bezier(0.16, 1, 0.3, 1)",
                pseudoElement: "::view-transition-new(root)",
            },
        );
    }).catch(() => {
        // A skipped transition is acceptable; the theme class has already been committed.
    });
}

function getVisibleThemeToggleOrigin(): { x: number; y: number } | undefined {
    const toggles = Array.from(window.document.querySelectorAll<HTMLElement>(".magic-theme-toggle"));
    let chosenRect: DOMRect | undefined;

    for (const toggle of toggles) {
        const rect = toggle.getBoundingClientRect();
        const style = window.getComputedStyle(toggle);
        if (
            rect.width <= 0 ||
            rect.height <= 0 ||
            style.display === "none" ||
            style.visibility === "hidden"
        ) {
            continue;
        }

        if (
            !chosenRect ||
            rect.bottom > chosenRect.bottom ||
            (rect.bottom === chosenRect.bottom && rect.left < chosenRect.left)
        ) {
            chosenRect = rect;
        }
    }

    if (!chosenRect) return undefined;
    return {
        x: chosenRect.left + chosenRect.width / 2,
        y: chosenRect.top + chosenRect.height / 2,
    };
}

function fallbackCircularThemeReveal(
    origin: { x: number; y: number },
    nextTheme: ResolvedTheme,
    commitTheme: () => void,
): Promise<void> {
    window.document
        .querySelectorAll(`.${THEME_REVEAL_OVERLAY_CLASS}`)
        .forEach((overlay) => overlay.remove());

    const endRadius = Math.hypot(
        Math.max(origin.x, window.innerWidth - origin.x),
        Math.max(origin.y, window.innerHeight - origin.y),
    );
    const overlay = window.document.createElement("div");
    overlay.className = THEME_REVEAL_OVERLAY_CLASS;
    overlay.style.background = nextTheme === "dark" ? "#1a1d23" : "#e5e7eb";
    overlay.style.clipPath = `circle(0px at ${origin.x}px ${origin.y}px)`;
    overlay.style.transition = `clip-path ${THEME_TRANSITION_DURATION_MS}ms cubic-bezier(0.16, 1, 0.3, 1)`;
    window.document.body.appendChild(overlay);

    return new Promise((resolve) => {
        let committed = false;
        const commitOnce = () => {
            if (committed) return;
            committed = true;
            commitTheme();
        };

        window.requestAnimationFrame(() => {
            overlay.style.clipPath = `circle(${endRadius}px at ${origin.x}px ${origin.y}px)`;
        });
        const commitTimeout = window.setTimeout(commitOnce, Math.round(THEME_TRANSITION_DURATION_MS * 0.62));
        window.setTimeout(() => {
            window.clearTimeout(commitTimeout);
            commitOnce();
            overlay.remove();
            resolve();
        }, THEME_TRANSITION_DURATION_MS + 80);
    });
}

async function applyDesktopWindowTheme(theme: ResolvedTheme) {
    if (!isTauriRuntime()) return;
    const failures: unknown[] = [];
    try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        await getCurrentWindow().setTheme(theme);
    } catch (error) {
        failures.push(error);
    }
    try {
        const { setTheme: setAppTheme } = await import("@tauri-apps/api/app");
        await setAppTheme(theme);
    } catch (error) {
        failures.push(error);
    }
    try {
        const { invoke } = await import("@tauri-apps/api/core");
        await invoke("set_desktop_window_chrome_theme", { theme });
    } catch (error) {
        failures.push(error);
    }
    if (failures.length === 3) {
        console.debug("Desktop window theme update failed.", failures[0]);
    }
}

export function ThemeProvider({
    children,
    defaultTheme = "system",
    storageKey = "scriber-theme",
    ...props
}: ThemeProviderProps) {
    const [theme, setTheme] = useState<Theme>(
        () => (localStorage.getItem(storageKey) as Theme) || defaultTheme
    );
    const [resolvedTheme, setResolvedTheme] = useState<ResolvedTheme>("dark");
    const deferredDesktopThemeRef = useRef<ResolvedTheme | null>(null);
    const revealGenerationRef = useRef(0);

    useEffect(() => {
        const effectiveTheme = resolveEffectiveTheme(theme);
        applyThemeClass(effectiveTheme);
        setResolvedTheme(effectiveTheme);
        if (deferredDesktopThemeRef.current === effectiveTheme) {
            return;
        }
        void applyDesktopWindowTheme(effectiveTheme);
    }, [theme]);

    // Listen for system theme changes
    useEffect(() => {
        if (theme !== "system") return;

        const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
        const handleChange = (e: MediaQueryListEvent) => {
            const newTheme = e.matches ? "dark" : "light";
            applyThemeClass(newTheme);
            setResolvedTheme(newTheme);
            void applyDesktopWindowTheme(newTheme);
        };

        mediaQuery.addEventListener("change", handleChange);
        return () => mediaQuery.removeEventListener("change", handleChange);
    }, [theme]);

    const updateTheme = useCallback((nextTheme: Theme, options?: ThemeTransitionOptions) => {
        localStorage.setItem(storageKey, nextTheme);

        const nextResolvedTheme = resolveEffectiveTheme(nextTheme);
        const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        const documentWithViewTransition = window.document as DocumentWithViewTransition;
        const startViewTransition = documentWithViewTransition.startViewTransition?.bind(documentWithViewTransition);
        const transitionOrigin = options?.origin ?? getVisibleThemeToggleOrigin();

        if (!transitionOrigin || prefersReducedMotion) {
            setTheme(nextTheme);
            return;
        }

        const commitTheme = () => {
            applyThemeClass(nextResolvedTheme);
            setResolvedTheme(nextResolvedTheme);
            setTheme(nextTheme);
        };

        const beginReveal = () => {
            const revealGeneration = revealGenerationRef.current + 1;
            revealGenerationRef.current = revealGeneration;
            deferredDesktopThemeRef.current = nextResolvedTheme;
            setThemeRevealActive(true);

            return () => {
                if (revealGenerationRef.current !== revealGeneration) return;
                setThemeRevealActive(false);
                if (deferredDesktopThemeRef.current === nextResolvedTheme) {
                    deferredDesktopThemeRef.current = null;
                }
                void applyDesktopWindowTheme(nextResolvedTheme);
            };
        };

        if (!startViewTransition) {
            const finishReveal = beginReveal();
            void fallbackCircularThemeReveal(transitionOrigin, nextResolvedTheme, commitTheme)
                .finally(finishReveal);
            return;
        }

        const finishReveal = beginReveal();
        const transition = startViewTransition(() => {
            commitTheme();
        });

        circularThemeReveal(transitionOrigin, transition);
        void transition.finished.then(finishReveal, finishReveal);
        window.setTimeout(finishReveal, THEME_TRANSITION_DURATION_MS + 140);
    }, [storageKey]);

    const value = useMemo(() => ({
        theme,
        setTheme: updateTheme,
        resolvedTheme,
    }), [theme, updateTheme, resolvedTheme]);

    return (
        <ThemeProviderContext.Provider {...props} value={value}>
            {children}
        </ThemeProviderContext.Provider>
    );
}

export const useTheme = () => {
    const context = useContext(ThemeProviderContext);

    if (context === undefined)
        throw new Error("useTheme must be used within a ThemeProvider");

    return context;
};
