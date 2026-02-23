import { useCallback, useState, useEffect, memo, useMemo, useRef } from "react";
import { useDropzone } from "react-dropzone";
import { UploadCloud, FileAudio, CheckCircle2, Loader2, XCircle, LayoutGrid, LayoutList, Square, Search, X } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Input } from "@/components/ui/input";
import { useLocation } from "wouter";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiUrl } from "@/lib/backend";
import { useToast } from "@/hooks/use-toast";
import { motion, useReducedMotion } from "framer-motion";
import { EmptyState } from "@/components/ui/empty-state";
import { SkeletonList } from "@/components/ui/skeleton-card";
import { QueryErrorState } from "@/components/ui/query-error-state";
import { useTranscriptAutoRefresh } from "@/hooks/use-transcript-auto-refresh";
import { useUrlQueryState } from "@/hooks/use-url-query-state";
import { DeleteActionButton } from "@/components/ui/delete-action-button";
import { CopyActionButton } from "@/components/ui/copy-action-button";

const DELETE_GLITCH_DURATION_MS = 1200;
const VIEW_MODE_STORAGE_KEY = "scriber:view-mode";

// Memoized FileCard to prevent unnecessary re-renders
interface FileCardProps {
  item: any;
  index: number;
  viewMode: "list" | "grid";
  isDeleting: boolean;
  isCopying: boolean;
  onDelete: (e: React.MouseEvent, id: string) => void;
  onCopy: (e: React.MouseEvent, id: string) => void;
  onNavigate: (id: string) => void;
  onHover?: (id: string) => void;
}

const FileCard = memo(function FileCard({
  item,
  index,
  viewMode,
  isDeleting,
  isCopying,
  onDelete,
  onCopy,
  onNavigate,
  onHover,
}: FileCardProps) {
  const prefersReducedMotion = useReducedMotion();
  const durationClass = "duration-[1200ms]";
  const listLayoutClasses = `grid transition-[grid-template-rows,margin-bottom] ease-in-out ${durationClass} ${isDeleting
    ? "grid-rows-[0fr] mb-0 overflow-hidden"
    : "grid-rows-[1fr] mb-4 last:mb-0 overflow-visible"
    }`;
  const layoutClasses = viewMode === "list" ? listLayoutClasses : "block";
  const visualClasses = `!transition-all !ease-out !duration-[1200ms] w-full origin-top transform-gpu ${isDeleting
    ? "hue-rotate-180 saturate-200 blur-md skew-x-[40deg] scale-y-50 translate-x-12 opacity-0"
    : "hue-rotate-0 saturate-100 blur-0 skew-x-0 scale-y-100 translate-x-0 opacity-100"
    }`;

  return (
    <motion.div
      layout="position"
      initial={prefersReducedMotion ? false : { opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        delay: Math.min(index * 0.02, 0.1),
        duration: prefersReducedMotion ? 0 : 0.2,
        ease: "easeOut",
        layout: { duration: prefersReducedMotion ? 0 : 0.45, ease: "easeInOut" },
      }}
      className={layoutClasses}
    >
      <Card
        className={`neu-recording-row perf-scroll-item ${viewMode === "grid" ? "perf-scroll-grid h-[220px]" : ""} p-4 rounded-[20px] cursor-pointer group ${visualClasses}`}
        onClick={() => onNavigate(item.id)}
        onMouseEnter={() => onHover?.(item.id)}
        role="button"
        tabIndex={0}
        aria-label={`Open transcript ${item.title}`}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onNavigate(item.id);
          }
        }}
      >
        {viewMode === "list" ? (
          // List view
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${item.status === 'failed'
                ? 'bg-red-50 dark:bg-red-900/20 text-red-600'
                : item.status === 'stopped'
                  ? 'bg-yellow-50 dark:bg-yellow-900/20 text-yellow-600'
                  : 'bg-gradient-to-br from-green-500/20 to-green-500/5 text-green-600'
                }`}>
                {item.status === 'failed' ? <XCircle className="w-5 h-5" /> : item.status === 'stopped' ? <Square className="w-5 h-5" /> : <FileAudio className="w-5 h-5" />}
              </div>
              <div>
                <h3 className="font-medium text-foreground group-hover:text-primary transition-colors">{item.title}</h3>
                <div className="flex items-center gap-3 text-xs text-muted-foreground mt-1">
                  {item.channel && <span>{item.channel}</span>}
                  {item.channel && <span>•</span>}
                  <span>{item.duration}</span>
                  <span>•</span>
                  <span>{item.date}</span>
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              {item.status === 'failed' ? (
                <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50 text-[10px]">Failed</Badge>
              ) : item.status === 'stopped' ? (
                <Badge variant="outline" className="text-yellow-600 border-yellow-200 bg-yellow-50 text-[10px]">Stopped</Badge>
              ) : (
                <div className="hidden sm:flex items-center gap-1 text-xs font-medium text-green-600 bg-green-50 px-2 py-1 rounded-full">
                  <CheckCircle2 className="w-3 h-3" />
                  Ready
                </div>
              )}
              <CopyActionButton
                onClick={(e) => onCopy(e, item.id)}
                disabled={isCopying}
                copied={isCopying}
                title="Copy transcript"
                ariaLabel={`Copy transcript ${item.title}`}
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
              <DeleteActionButton
                onClick={(e) => onDelete(e, item.id)}
                disabled={isDeleting}
                loading={isDeleting}
                title="Delete transcript"
                ariaLabel={`Delete transcript ${item.title}`}
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
            </div>
          </div>
        ) : (
          // Grid view
          <div className="flex flex-col h-full">
            <div className="flex items-start justify-between mb-3">
              <div className={`w-12 h-12 rounded-xl flex items-center justify-center ${item.status === 'failed'
                ? 'bg-red-50 dark:bg-red-900/20 text-red-600'
                : item.status === 'stopped'
                  ? 'bg-yellow-50 dark:bg-yellow-900/20 text-yellow-600'
                  : 'bg-gradient-to-br from-green-500/20 to-green-500/5 text-green-600'
                }`}>
                {item.status === 'failed' ? <XCircle className="w-6 h-6" /> : item.status === 'stopped' ? <Square className="w-6 h-6" /> : <FileAudio className="w-6 h-6" />}
              </div>
              <div className="flex items-center gap-1">
                {item.status === 'failed' ? (
                  <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50 text-[10px]">Failed</Badge>
                ) : item.status === 'stopped' ? (
                  <Badge variant="outline" className="text-yellow-600 border-yellow-200 bg-yellow-50 text-[10px]">Stopped</Badge>
                ) : (
                  <div className="flex items-center gap-1 text-xs font-medium text-green-600 bg-green-50 px-2 py-1 rounded-full">
                    <CheckCircle2 className="w-3 h-3" />
                  </div>
                )}
              </div>
            </div>
            <h3 className="font-medium text-foreground group-hover:text-primary transition-colors line-clamp-2 mb-2">{item.title}</h3>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground mt-auto">
              <span>{item.duration}</span>
              <span>•</span>
              <span>{item.date}</span>
            </div>
            <div className="flex items-center justify-end mt-2 gap-1">
              <CopyActionButton
                onClick={(e) => onCopy(e, item.id)}
                disabled={isCopying}
                copied={isCopying}
                title="Copy transcript"
                ariaLabel={`Copy transcript ${item.title}`}
                size="sm"
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
              <DeleteActionButton
                onClick={(e) => onDelete(e, item.id)}
                disabled={isDeleting}
                loading={isDeleting}
                title="Delete transcript"
                ariaLabel={`Delete transcript ${item.title}`}
                size="sm"
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
            </div>
          </div>
        )}
      </Card>
    </motion.div>
  );
});

export default function FileTranscribe() {
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const [isUploading, setIsUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadingFileName, setUploadingFileName] = useState("");
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [copyingId, setCopyingId] = useState<string | null>(null);
  const deletingRef = useRef<string | null>(null);
  const queryClient = useQueryClient();
  const getInitialViewMode = () => {
    if (typeof window === "undefined") return "list" as const;
    const stored = window.localStorage.getItem(VIEW_MODE_STORAGE_KEY);
    if (stored === "list" || stored === "grid") return stored;
    return "list" as const;
  };
  const initialViewMode = getInitialViewMode();
  const [viewMode, setViewMode] = useUrlQueryState<"list" | "grid">("view", initialViewMode, {
    parse: (raw) => (raw === "list" || raw === "grid" ? raw : initialViewMode),
  });

  // Search state
  const [searchQuery, setSearchQuery] = useUrlQueryState("q", "", {
    parse: (raw) => raw ?? "",
    serialize: (value) => {
      const trimmed = value.trim();
      return trimmed ? trimmed : null;
    },
    syncDelayMs: 250,
  });
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const transcriptsQueryKey = useMemo(
    () => ["/api/transcripts", { q: debouncedSearch, type: "file" }] as const,
    [debouncedSearch],
  );

  // Debounce search
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(searchQuery), 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(VIEW_MODE_STORAGE_KEY, viewMode);
  }, [viewMode]);

  const transcriptsQuery = useQuery({
    queryKey: transcriptsQueryKey,
    queryFn: async () => {
      const params = new URLSearchParams();
      if (debouncedSearch) params.set("q", debouncedSearch);
      params.set("type", "file");
      const res = await fetch(apiUrl(`/api/transcripts?${params}`), { credentials: "include" });
      return res.json();
    },
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    placeholderData: (previous) => previous,
  });
  const recentFromBackend: any[] = (transcriptsQuery.data as any)?.items || [];

  useTranscriptAutoRefresh({
    queryKey: transcriptsQueryKey,
    onError: (message) => {
      toast({
        title: "Transcription Error",
        description: message,
        variant: "destructive",
        duration: 6000,
      });
    },
  });

  const uploadFile = async (file: File) => {
    setIsUploading(true);
    setUploadingFileName(file.name);
    setUploadProgress(10);

    try {
      const formData = new FormData();
      formData.append("file", file);

      setUploadProgress(30);

      const res = await fetch(apiUrl("/api/file/transcribe"), {
        method: "POST",
        credentials: "include",
        body: formData,
      });

      setUploadProgress(80);

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.message || res.statusText);
      }

      const rec = await res.json();
      setUploadProgress(100);

      toast({
        title: "File uploaded",
        description: "Transcription started...",
        duration: 3000,
      });

      // Navigate to the transcript detail page
      if (rec?.id) {
        setLocation(`/transcript/${rec.id}`);
      }
    } catch (e: any) {
      toast({
        title: "Upload failed",
        description: String(e?.message || e),
        duration: 5000,
      });
    } finally {
      setIsUploading(false);
      setUploadProgress(0);
      setUploadingFileName("");
    }
  };

  const deleteTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
    e.stopPropagation(); // Prevent card click navigation
    if (deletingRef.current) return;

    deletingRef.current = id;
    setDeletingId(id);
    try {
      await new Promise((resolve) => setTimeout(resolve, DELETE_GLITCH_DURATION_MS));

      const res = await fetch(apiUrl(`/api/transcripts/${id}`), {
        method: "DELETE",
        credentials: "include",
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.message || res.statusText);
      }
      toast({
        title: "Deleted",
        description: "Transcript removed successfully.",
        duration: 2000,
      });
      queryClient.invalidateQueries({
        predicate: (query) =>
          query.queryKey[0] === "/api/transcripts" &&
          (query.queryKey[1] as { type?: string })?.type === "file",
      });
    } catch (e: any) {
      toast({
        title: "Delete failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      deletingRef.current = null;
      setDeletingId(null);
    }
  }, [queryClient, toast]);

  const copyTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    if (copyingId) return;

    setCopyingId(id);
    try {
      // Fetch the full transcript content
      const res = await fetch(apiUrl(`/api/transcripts/${id}`), {
        credentials: "include",
      });
      if (!res.ok) {
        throw new Error(res.statusText);
      }
      const data = await res.json();
      const content = data?.content || "";
      if (!content) {
        throw new Error("No transcript content available");
      }
      await navigator.clipboard.writeText(content);
      toast({
        title: "Copied",
        description: "Transcript copied to clipboard.",
        duration: 2000,
      });
      // Show check mark briefly
      setTimeout(() => setCopyingId(null), 1500);
    } catch (e: any) {
      toast({
        title: "Copy failed",
        description: String(e?.message || e),
        duration: 4000,
      });
      setCopyingId(null);
    }
  }, [copyingId, toast]);

  const navigateToTranscript = useCallback((id: string) => {
    setLocation(`/transcript/${id}`);
  }, [setLocation]);

  // Preload TranscriptDetail page and data on hover for instant navigation
  const preloadTranscript = useCallback((id: string) => {
    import("@/pages/TranscriptDetail");
    queryClient.prefetchQuery({ queryKey: ["/api/transcripts", id] });
  }, [queryClient]);

  const onDrop = useCallback((acceptedFiles: File[]) => {
    if (acceptedFiles.length > 0 && !isUploading) {
      uploadFile(acceptedFiles[0]);
    }
  }, [isUploading]);

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      "audio/*": [".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"],
      "video/*": [".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"],
    },
    multiple: false,
    disabled: isUploading,
  });

  // Separate processing items from completed
  const processingItems = recentFromBackend.filter((t: any) => t.status === "processing");
  const completedItems = recentFromBackend.filter((t: any) => t.status !== "processing");

  return (
    <div className="max-w-screen-md mx-auto px-4 py-6 md:py-8">
      <header className="mb-6 space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-foreground">Import File</h1>
        <p className="text-muted-foreground">Upload audio or video files for transcription</p>
      </header>

      {/* Dropzone - Debossed neumorphic style */}
      <div
        {...getRootProps({
          role: "button",
          "aria-label": "Upload file for transcription",
        })}
        className={`
          neu-status-well rounded-xl p-10 text-center cursor-pointer transition-all duration-200 mb-6
          flex flex-col items-center justify-center gap-4 group
          ${isDragActive ? 'ring-2 ring-primary' : ''}
          ${isUploading ? 'opacity-50 pointer-events-none' : ''}
        `}
      >
        <input {...getInputProps()} />
        <div className={`p-4 rounded-full bg-background shadow-sm transition-transform duration-200 ${isDragActive ? 'scale-110' : 'group-hover:scale-110'}`}>
          {isUploading ? (
            <Loader2 className="w-8 h-8 text-primary animate-spin" />
          ) : (
            <UploadCloud className={`w-8 h-8 ${isDragActive ? 'text-primary' : 'text-muted-foreground'}`} />
          )}
        </div>
        <div className="space-y-1">
          {isUploading ? (
            <>
              <p className="text-lg font-medium">Uploading {uploadingFileName}...</p>
              <Progress value={uploadProgress} className="h-2 w-48 mx-auto mt-2" />
            </>
          ) : (
            <>
              <p className="text-lg font-medium">Click to upload or drag and drop</p>
              <p className="text-sm text-muted-foreground">Audio: MP3, M4A, WAV (max 200MB) • Video: MP4, MOV, etc. (max 2GB, audio extracted)</p>
            </>
          )}
        </div>
      </div>

      {/* Processing Queue */}
      {processingItems.length > 0 && (
        <div className="mb-6 space-y-4">
          <div className="flex items-center justify-between px-1">
            <h3 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider">Processing Queue</h3>
          </div>
          {processingItems.map((item: any) => (
            <Card key={item.id} className="neu-recording-row perf-scroll-item p-4 rounded-[20px]">
              <div className="flex items-center gap-4">
                <div className="p-2 bg-blue-50 dark:bg-blue-900/20 text-blue-600 rounded-lg">
                  <FileAudio className="w-5 h-5" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex justify-between mb-1">
                    <span className="font-medium text-sm truncate">{item.title}</span>
                    <Badge variant="outline" className="text-blue-600 border-blue-200 bg-blue-50 text-[10px] flex items-center gap-1">
                      <Loader2 className="w-3 h-3 animate-spin" />
                      {item.step || "Processing"}
                    </Badge>
                  </div>
                  <p className="text-xs text-muted-foreground">{item.channel || ""}</p>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-muted-foreground hover:text-foreground"
                  type="button"
                  aria-label={`View transcript ${item.title}`}
                  onClick={() => setLocation(`/transcript/${item.id}`)}
                >
                  View
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* History */}
      <div className="space-y-4">
        <div className="flex items-center justify-between px-1">
          <h2 className="text-lg font-semibold">Recent Files</h2>
          <ToggleGroup
            type="single"
            value={viewMode}
            onValueChange={(val) => val && setViewMode(val as "list" | "grid")}
            className="bg-secondary/50 rounded-lg p-1"
          >
            <ToggleGroupItem value="list" aria-label="List view" className="h-8 w-8 p-0">
              <LayoutList className="h-4 w-4" />
            </ToggleGroupItem>
            <ToggleGroupItem value="grid" aria-label="Grid view" className="h-8 w-8 p-0">
              <LayoutGrid className="h-4 w-4" />
            </ToggleGroupItem>
          </ToggleGroup>
        </div>

        {/* Search Bar */}
        <div className="relative mt-3">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <Input
            type="text"
            placeholder="Search files..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-9 pr-9 h-9 bg-secondary/50"
          />
          {searchQuery && (
            <button
              type="button"
              onClick={() => setSearchQuery("")}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              aria-label="Clear file search"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
        {transcriptsQuery.isLoading ? (
          <SkeletonList count={3} variant={viewMode} />
        ) : transcriptsQuery.isError ? (
          <QueryErrorState
            title="Could not load file transcripts"
            description="Please retry loading your file history."
            onRetry={() => transcriptsQuery.refetch()}
          />
        ) : completedItems.length === 0 ? (
          debouncedSearch ? (
            <p className="text-center text-muted-foreground py-8">No files match "{debouncedSearch}"</p>
          ) : (
            <EmptyState type="file" />
          )
        ) : (
          <div className={viewMode === "grid" ? "grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 items-stretch" : "flex flex-col"}>
            {completedItems.map((item: any, index: number) => (
              <FileCard
                key={item.id}
                item={item}
                index={index}
                viewMode={viewMode}
                isDeleting={deletingId === item.id}
                isCopying={copyingId === item.id}
                onDelete={deleteTranscript}
                onCopy={copyTranscript}
                onNavigate={navigateToTranscript}
                onHover={preloadTranscript}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

