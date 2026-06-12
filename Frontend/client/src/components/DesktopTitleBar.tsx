import { Minus, Square, X } from "lucide-react";
import { useCallback } from "react";
import { isTauriRuntime } from "@/lib/backend";
import { cn } from "@/lib/utils";

type TauriWindowControls = {
    minimize: () => Promise<void>;
    toggleMaximize: () => Promise<void>;
    close: () => Promise<void>;
};

type WindowCommand = keyof TauriWindowControls;

async function runWindowCommand(command: WindowCommand) {
    if (!isTauriRuntime()) return;
    try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        const currentWindow = getCurrentWindow() as TauriWindowControls;
        await currentWindow[command]();
    } catch (error) {
        console.debug("Window chrome command failed.", error);
    }
}

export function DesktopTitleBar() {
    const handleMinimize = useCallback(() => {
        void runWindowCommand("minimize");
    }, []);

    const handleMaximize = useCallback(() => {
        void runWindowCommand("toggleMaximize");
    }, []);

    const handleClose = useCallback(() => {
        void runWindowCommand("close");
    }, []);

    if (!isTauriRuntime()) return null;

    return (
        <div className="desktop-titlebar hidden md:flex" data-tauri-drag-region="true">
            <div
                className="desktop-titlebar__drag-region"
                data-tauri-drag-region="true"
                onDoubleClick={handleMaximize}
            />
            <div className="desktop-titlebar__controls" aria-label="Window controls">
                <button
                    type="button"
                    aria-label="Minimize window"
                    className="desktop-titlebar__button"
                    onClick={handleMinimize}
                >
                    <Minus className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
                <button
                    type="button"
                    aria-label="Maximize window"
                    className="desktop-titlebar__button"
                    onClick={handleMaximize}
                >
                    <Square className="h-3 w-3" aria-hidden="true" />
                </button>
                <button
                    type="button"
                    aria-label="Close window"
                    className={cn("desktop-titlebar__button", "desktop-titlebar__button--close")}
                    onClick={handleClose}
                >
                    <X className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
            </div>
        </div>
    );
}
