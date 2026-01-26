import { useState, useCallback } from "react";
import { useLocation } from "wouter";
import { useQuery } from "@tanstack/react-query";
import {
  CommandDialog,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandShortcut,
  CommandSeparator,
} from "@/components/ui/command";
import {
  Mic,
  Square,
  Settings,
  Youtube,
  FolderOpen,
  Home,
} from "lucide-react";
import { apiUrl } from "@/lib/backend";
import { useSharedWebSocket } from "@/contexts/WebSocketContext";
import { useToast } from "@/hooks/use-toast";

interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

interface Transcript {
  id: number;
  title: string;
  content: string;
  createdAt: string;
  type: string;
}

interface SettingsResponse {
  hotkey?: string;
  hotkeyRaw?: string;
}

export function CommandPalette({ open, onOpenChange }: CommandPaletteProps) {
  const [, setLocation] = useLocation();
  const [isRecording, setIsRecording] = useState(false);
  const { toast } = useToast();

  // Track recording state via WebSocket
  const handleWsMessage = useCallback((msg: any) => {
    if (!msg || typeof msg !== "object") return;

    switch (msg.type) {
      case "state":
      case "status":
        setIsRecording(!!msg.listening);
        break;
      case "session_started":
        setIsRecording(true);
        break;
      case "session_finished":
        setIsRecording(false);
        break;
    }
  }, []);

  useSharedWebSocket(handleWsMessage);

  // Load settings to get the configured hotkey
  const { data: settings } = useQuery<SettingsResponse>({
    queryKey: ["/api/settings"],
    queryFn: async () => {
      const res = await fetch(apiUrl("/api/settings"), {
        credentials: "include",
      });
      if (!res.ok) throw new Error("Failed to load settings");
      return res.json();
    },
    staleTime: 60000, // Cache for 1 minute
  });

  // Get display hotkey from settings
  const recordingHotkey = settings?.hotkey || settings?.hotkeyRaw || "";

  // Load transcripts for search (more items for better search)
  const { data: transcriptsData } = useQuery<{ items: Transcript[] }>({
    queryKey: ["/api/transcripts", { limit: 50 }],
    queryFn: async () => {
      const res = await fetch(apiUrl("/api/transcripts?limit=50"), {
        credentials: "include",
      });
      if (!res.ok) throw new Error("Failed to load transcripts");
      return res.json();
    },
    enabled: open, // Only fetch when palette is open
    staleTime: 30000, // Cache for 30 seconds
  });

  const transcripts = transcriptsData?.items || [];

  // Navigation helper
  const navigate = (path: string) => {
    setLocation(path);
    onOpenChange(false);
  };

  // Toggle recording
  const handleToggleRecording = async () => {
    try {
      const endpoint = isRecording ? "/api/live-mic/stop" : "/api/live-mic/start";
      const res = await fetch(apiUrl(endpoint), {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || res.statusText);
      }
      onOpenChange(false);
    } catch (e: any) {
      toast({
        title: "Action failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  // Format date for display
  const formatDate = (dateStr: string) => {
    if (!dateStr) return "";
    const date = new Date(dateStr);
    if (isNaN(date.getTime())) return "";
    return date.toLocaleDateString("de-DE", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
    });
  };

  // Get icon for transcript type
  const getTranscriptIcon = (type: string) => {
    switch (type) {
      case "youtube":
        return Youtube;
      case "file":
        return FolderOpen;
      default:
        return Mic;
    }
  };

  // Get display title for transcript
  const getDisplayTitle = (transcript: Transcript) => {
    if (transcript.title) return transcript.title;
    if (transcript.content) {
      const preview = transcript.content.slice(0, 50);
      return preview + (transcript.content.length > 50 ? "..." : "");
    }
    return `Transkript #${transcript.id}`;
  };

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange}>
      <CommandInput placeholder="Befehl eingeben oder Transkript suchen..." />
      <CommandList>
        <CommandEmpty>Keine Ergebnisse gefunden.</CommandEmpty>

        {/* Actions */}
        <CommandGroup heading="Aktionen">
          {isRecording ? (
            <CommandItem onSelect={handleToggleRecording}>
              <Square className="mr-2 h-4 w-4 text-red-500" />
              <span>Aufnahme stoppen</span>
              {recordingHotkey && <CommandShortcut>{recordingHotkey}</CommandShortcut>}
            </CommandItem>
          ) : (
            <CommandItem onSelect={handleToggleRecording}>
              <Mic className="mr-2 h-4 w-4" />
              <span>Aufnahme starten</span>
              {recordingHotkey && <CommandShortcut>{recordingHotkey}</CommandShortcut>}
            </CommandItem>
          )}
        </CommandGroup>

        <CommandSeparator />

        {/* Navigation */}
        <CommandGroup heading="Navigation">
          <CommandItem onSelect={() => navigate("/")}>
            <Home className="mr-2 h-4 w-4" />
            <span>Live Mikrofon</span>
          </CommandItem>
          <CommandItem onSelect={() => navigate("/youtube")}>
            <Youtube className="mr-2 h-4 w-4" />
            <span>YouTube</span>
          </CommandItem>
          <CommandItem onSelect={() => navigate("/file")}>
            <FolderOpen className="mr-2 h-4 w-4" />
            <span>Datei-Upload</span>
          </CommandItem>
          <CommandItem onSelect={() => navigate("/settings")}>
            <Settings className="mr-2 h-4 w-4" />
            <span>Einstellungen</span>
          </CommandItem>
        </CommandGroup>

        {/* Transcripts - searchable by title */}
        {transcripts.length > 0 && (
          <>
            <CommandSeparator />
            <CommandGroup heading="Transkripte">
              {transcripts.map((transcript) => {
                const Icon = getTranscriptIcon(transcript.type);
                const displayTitle = getDisplayTitle(transcript);

                return (
                  <CommandItem
                    key={transcript.id}
                    value={`transcript ${displayTitle} ${transcript.title || ""}`}
                    onSelect={() => navigate(`/transcript/${transcript.id}`)}
                  >
                    <Icon className="mr-2 h-4 w-4" />
                    <span className="flex-1 truncate">{displayTitle}</span>
                    <span className="ml-2 text-xs text-muted-foreground">
                      {formatDate(transcript.createdAt)}
                    </span>
                  </CommandItem>
                );
              })}
            </CommandGroup>
          </>
        )}
      </CommandList>
    </CommandDialog>
  );
}
