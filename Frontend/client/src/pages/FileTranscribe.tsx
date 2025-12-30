import { useCallback, useState, useEffect } from "react";
import { useDropzone } from "react-dropzone";
import { UploadCloud, FileAudio, FileVideo, CheckCircle2, Clock, MoreVertical, Loader2, XCircle, Trash2, LayoutGrid, LayoutList } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useLocation } from "wouter";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiUrl, wsUrl } from "@/lib/backend";
import { useToast } from "@/hooks/use-toast";
import { motion } from "framer-motion";
import { EmptyState } from "@/components/ui/empty-state";
import { SkeletonList } from "@/components/ui/skeleton-card";

export default function FileTranscribe() {
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const [isUploading, setIsUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadingFileName, setUploadingFileName] = useState("");
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const [viewMode, setViewMode] = useState<"list" | "grid">(
    () => (localStorage.getItem("scriber-view-mode") as "list" | "grid") || "list"
  );

  // Persist view mode
  useEffect(() => {
    localStorage.setItem("scriber-view-mode", viewMode);
  }, [viewMode]);

  const transcriptsQuery = useQuery({
    queryKey: ["/api/transcripts"],
    staleTime: 0, // Always fetch fresh data on mount
  });
  const recentFromBackend: any[] = ((transcriptsQuery.data as any)?.items || []).filter(
    (t: any) => t?.type === "file",
  );

  // WebSocket for real-time updates
  useEffect(() => {
    const ws = new WebSocket(wsUrl("/ws"));

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg?.type === "history_updated") {
          queryClient.refetchQueries({ queryKey: ["/api/transcripts"] });
        }
      } catch {
        // ignore
      }
    };

    return () => {
      try {
        ws.close();
      } catch {
        // ignore
      }
    };
  }, [queryClient]);

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

  const deleteTranscript = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation(); // Prevent card click navigation
    if (deletingId) return;

    setDeletingId(id);
    try {
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
    } catch (e: any) {
      toast({
        title: "Delete failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      setDeletingId(null);
    }
  };

  const onDrop = useCallback((acceptedFiles: File[]) => {
    if (acceptedFiles.length > 0 && !isUploading) {
      uploadFile(acceptedFiles[0]);
    }
  }, [isUploading]);

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      "audio/*": [".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"],
      "video/*": [".mp4", ".mov", ".webm"],
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
        {...getRootProps()}
        className={`
          neu-search-inset rounded-xl p-10 text-center cursor-pointer transition-all duration-200 mb-6
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
              <p className="text-sm text-muted-foreground">MP3, M4A, WAV, MP4, MOV (max 2GB)</p>
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
            <Card key={item.id} className="neu-panel-raised p-4 border-0 bg-card">
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
        {transcriptsQuery.isLoading ? (
          <SkeletonList count={3} variant={viewMode} />
        ) : completedItems.length === 0 ? (
          <EmptyState type="file" />
        ) : (
          <div className={viewMode === "grid" ? "grid grid-cols-2 gap-3" : "grid gap-3"}>
            {completedItems.map((item: any, index: number) => (
              <motion.div
                key={item.id}
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: index * 0.05, duration: 0.3, ease: "easeOut" }}
              >
                <Card className="neu-panel-raised p-4 hover:scale-[1.01] transition-all cursor-pointer bg-card border-0 group" onClick={() => setLocation(`/transcript/${item.id}`)}>
                  {viewMode === "list" ? (
                    // List view
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-4">
                        <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${item.status === 'failed'
                          ? 'bg-red-50 dark:bg-red-900/20 text-red-600'
                          : 'bg-gradient-to-br from-green-500/20 to-green-500/5 text-green-600'
                          }`}>
                          {item.status === 'failed' ? <XCircle className="w-5 h-5" /> : <FileAudio className="w-5 h-5" />}
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
                        ) : (
                          <div className="hidden sm:flex items-center gap-1 text-xs font-medium text-green-600 bg-green-50 px-2 py-1 rounded-full">
                            <CheckCircle2 className="w-3 h-3" />
                            Ready
                          </div>
                        )}
                        <Button
                          variant="ghost"
                          size="icon"
                          className="opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
                          onClick={(e) => deleteTranscript(e, item.id)}
                          disabled={deletingId === item.id}
                        >
                          {deletingId === item.id ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                          ) : (
                            <Trash2 className="w-4 h-4" />
                          )}
                        </Button>
                      </div>
                    </div>
                  ) : (
                    // Grid view
                    <div className="flex flex-col h-full">
                      <div className="flex items-start justify-between mb-3">
                        <div className={`w-12 h-12 rounded-xl flex items-center justify-center ${item.status === 'failed'
                          ? 'bg-red-50 dark:bg-red-900/20 text-red-600'
                          : 'bg-gradient-to-br from-green-500/20 to-green-500/5 text-green-600'
                          }`}>
                          {item.status === 'failed' ? <XCircle className="w-6 h-6" /> : <FileAudio className="w-6 h-6" />}
                        </div>
                        <div className="flex items-center gap-1">
                          {item.status === 'failed' ? (
                            <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50 text-[10px]">Failed</Badge>
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
                      <div className="flex items-center justify-end mt-2">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6 opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
                          onClick={(e) => deleteTranscript(e, item.id)}
                          disabled={deletingId === item.id}
                        >
                          {deletingId === item.id ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                          ) : (
                            <Trash2 className="w-3 h-3" />
                          )}
                        </Button>
                      </div>
                    </div>
                  )}
                </Card>
              </motion.div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

