import { useParams, Link, useLocation } from "wouter";
import { ArrowLeft, Share2, Download, Copy, Play, Search, Clock, Calendar, Pencil, Check, Loader2, Sparkles, FileText, Square, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { MOCK_TRANSCRIPTS } from "@/lib/mockData";
import { useToast } from "@/hooks/use-toast";
import { useState, useEffect, useRef, useLayoutEffect, useCallback, useMemo } from "react";
import { apiUrl } from "@/lib/backend";
import ReactMarkdown from "react-markdown";
import { QueryErrorState } from "@/components/ui/query-error-state";
import { useTranscriptAutoRefresh } from "@/hooks/use-transcript-auto-refresh";
import { extractFailureMessage, friendlyError, friendlyRequestMessage, responseErrorMessage } from "@/lib/request-errors";

// Speaker colors for diarization - visually distinct palette
const SPEAKER_COLORS = [
  { bg: "bg-blue-100 dark:bg-blue-900/40", text: "text-blue-700 dark:text-blue-300", border: "border-blue-300 dark:border-blue-700" },
  { bg: "bg-emerald-100 dark:bg-emerald-900/40", text: "text-emerald-700 dark:text-emerald-300", border: "border-emerald-300 dark:border-emerald-700" },
  { bg: "bg-amber-100 dark:bg-amber-900/40", text: "text-amber-700 dark:text-amber-300", border: "border-amber-300 dark:border-amber-700" },
  { bg: "bg-purple-100 dark:bg-purple-900/40", text: "text-purple-700 dark:text-purple-300", border: "border-purple-300 dark:border-purple-700" },
  { bg: "bg-rose-100 dark:bg-rose-900/40", text: "text-rose-700 dark:text-rose-300", border: "border-rose-300 dark:border-rose-700" },
  { bg: "bg-cyan-100 dark:bg-cyan-900/40", text: "text-cyan-700 dark:text-cyan-300", border: "border-cyan-300 dark:border-cyan-700" },
];

// Component to render transcript with speaker diarization labels
function SpeakerFormattedText({ content }: { content: string }) {
  const hasSpeakerLabels = useMemo(
    () => /\[Speaker (\d+)\]:/.test(content),
    [content]
  );
  const paragraphs = useMemo(() => (content || "").split(/\n\n+/), [content]);
  const parsed = useMemo(() => {
    if (!hasSpeakerLabels) return [];
    const labelPattern = /^\[Speaker (\d+)\]:\s*([\s\S]*)$/;
    return paragraphs.map((para) => {
      const labelMatch = labelPattern.exec(para);
      if (!labelMatch) {
        return { type: "text" as const, text: para };
      }
      return {
        type: "speaker" as const,
        speakerNum: parseInt(labelMatch[1], 10),
        text: labelMatch[2],
      };
    });
  }, [hasSpeakerLabels, paragraphs]);

  if (!hasSpeakerLabels) {
    return <span>{content}</span>;
  }

  return (
    <div className="space-y-4">
      {parsed.map((segment, idx) => {
        if (segment.type === "speaker") {
          const colorIdx = (segment.speakerNum - 1) % SPEAKER_COLORS.length;
          const colors = SPEAKER_COLORS[colorIdx];

          return (
            <div key={idx} className="flex flex-col gap-1">
              <span
                className={`inline-flex items-center self-start px-2.5 py-0.5 rounded-full text-xs font-medium border ${colors.bg} ${colors.text} ${colors.border}`}
              >
                Speaker {segment.speakerNum}
              </span>
              <p className="leading-relaxed">{segment.text}</p>
            </div>
          );
        }
        return <p key={idx} className="leading-relaxed">{segment.text}</p>;
      })}
    </div>
  );
}

// FitText component that dynamically scales font size to fit container
interface FitTextProps {
  children: string;
  minFontSize?: number;
  maxFontSize?: number;
  className?: string;
}

function FitText({ children, minFontSize = 12, maxFontSize = 24, className = "" }: FitTextProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLSpanElement>(null);
  const [fontSize, setFontSize] = useState(maxFontSize);

  const calculateFit = useCallback(() => {
    const container = containerRef.current;
    const measureSpan = measureRef.current;
    if (!container || !measureSpan) return;

    const containerWidth = container.offsetWidth;
    if (containerWidth === 0) return;

    measureSpan.style.fontSize = `${maxFontSize}px`;
    const textWidth = measureSpan.offsetWidth;

    if (textWidth > containerWidth) {
      // Calculate the scale factor and apply it
      const scale = containerWidth / textWidth;
      const newSize = Math.max(minFontSize, Math.floor(maxFontSize * scale));
      setFontSize(newSize);
    } else {
      setFontSize(maxFontSize);
    }
  }, [children, maxFontSize, minFontSize]);

  // Calculate on mount and when children change
  useLayoutEffect(() => {
    // Small delay to ensure container is rendered
    const timer = requestAnimationFrame(() => {
      calculateFit();
    });
    return () => cancelAnimationFrame(timer);
  }, [children, calculateFit]);

  // Recalculate on resize
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const resizeObserver = new ResizeObserver(() => {
      calculateFit();
    });

    resizeObserver.observe(container);
    return () => resizeObserver.disconnect();
  }, [calculateFit]);

  return (
    <div ref={containerRef} className="w-full relative">
      <span
        ref={measureRef}
        aria-hidden="true"
        className="pointer-events-none absolute opacity-0 whitespace-nowrap font-bold tracking-tight"
        style={{ fontSize: `${maxFontSize}px` }}
      >
        {children}
      </span>
      <span
        className={className}
        style={{
          fontSize: `${fontSize}px`,
          display: 'block',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis'
        }}
        title={children}
      >
        {children}
      </span>
    </div>
  );
}

function DurationText({
  status,
  duration,
  startedAt,
}: {
  status?: string;
  duration?: string;
  startedAt?: string;
}) {
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const fallbackStartedAtRef = useRef<number | null>(null);

  const parsedStartMs = useMemo(() => {
    const raw = (startedAt || "").trim();
    if (!raw) return null;
    const normalized = raw.includes("T") ? raw : raw.replace(" ", "T");
    const millis = Date.parse(normalized);
    return Number.isFinite(millis) ? millis : null;
  }, [startedAt]);

  const formatElapsed = useCallback((seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
  }, []);

  useEffect(() => {
    if (status !== "processing") {
      fallbackStartedAtRef.current = null;
      setElapsedSeconds(0);
      return;
    }

    if (parsedStartMs === null && fallbackStartedAtRef.current === null) {
      fallbackStartedAtRef.current = Date.now();
    }

    const updateElapsed = () => {
      const base = parsedStartMs ?? fallbackStartedAtRef.current;
      if (base === null) {
        setElapsedSeconds(0);
        return;
      }
      const elapsed = Math.max(0, Math.floor((Date.now() - base) / 1000));
      setElapsedSeconds(elapsed);
    };

    updateElapsed();
    const interval = setInterval(updateElapsed, 1000);
    return () => clearInterval(interval);
  }, [parsedStartMs, status]);

  const display = status === "processing" ? formatElapsed(elapsedSeconds) : (duration || "");
  return <span>{display}</span>;
}

function StopButton({ transcriptId, onStop }: { transcriptId: string; onStop: () => void }) {
  const [isStopping, setIsStopping] = useState(false);
  const { toast } = useToast();

  const handleStop = async () => {
    if (isStopping) return;
    setIsStopping(true);
    try {
      const res = await fetch(apiUrl(`/api/transcripts/${transcriptId}/cancel`), {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) throw new Error("Failed to stop");

      toast({ title: "Stopping...", description: "Task cancellation requested." });
      onStop();
    } catch {
      toast({ title: "Error", description: "Failed to stop task.", variant: "destructive" });
      setIsStopping(false);
    }
  };

  return (
    <Button size="sm" variant="destructive" onClick={handleStop} disabled={isStopping} type="button">
      {isStopping ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <Square className="w-3 h-3 mr-1 fill-current" />}
      Stop
    </Button>
  );
}

function SummarizeButton({ transcriptId, onComplete }: { transcriptId: string | undefined; onComplete: () => void }) {
  const [isSummarizing, setIsSummarizing] = useState(false);
  const { toast } = useToast();

  const handleSummarize = async () => {
    if (!transcriptId || isSummarizing) return;
    setIsSummarizing(true);
    try {
      const res = await fetch(apiUrl(`/api/transcripts/${transcriptId}/summarize`), {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.message || res.statusText);
      }
      toast({
        title: "Summary generated",
        description: "The transcript has been summarized.",
        duration: 3000,
      });
      onComplete();
    } catch (e: any) {
      toast({
        title: "Summarization failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      setIsSummarizing(false);
    }
  };

  return (
    <Button size="sm" variant="outline" onClick={handleSummarize} disabled={isSummarizing} type="button">
      {isSummarizing ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <Sparkles className="w-3 h-3 mr-1" />}
      {isSummarizing ? "Summarizing..." : "Summarize"}
    </Button>
  );
}

export default function TranscriptDetail() {
  const { id } = useParams();
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const [copied, setCopied] = useState(false);
  const [isRetryingYoutube, setIsRetryingYoutube] = useState(false);
  const queryClient = useQueryClient();
  const { isWsConnected } = useTranscriptAutoRefresh({
    transcriptId: id,
    onError: (message) => {
      toast({
        title: "Error",
        description: message,
        variant: "destructive",
        duration: 6000,
      });
    },
  });

  const transcriptQuery = useQuery({
    queryKey: ["/api/transcripts", id],
    enabled: !!id,
    refetchInterval: (query: any) => {
      const data = query?.state?.data as any;
      const status = data?.status;
      const isActive = status === "processing" || status === "recording";
      if (isActive) {
        // Keep a light polling fallback even with WS connected in case
        // a WS event is missed or delayed.
        return isWsConnected ? 3000 : 1000;
      }
      // If no data yet (or temporary fetch failure), keep retrying.
      if (!data) {
        return 1500;
      }
      return false;
    },
  });

  // Fetch settings to check if auto-summarize is enabled
  const settingsQuery = useQuery({
    queryKey: ["/api/settings"],
    queryFn: async () => {
      const res = await fetch(apiUrl("/api/settings"), { credentials: "include" });
      if (!res.ok) return {};
      return res.json();
    },
  });
  const autoSummarize = settingsQuery.data?.autoSummarize === true;
  const mock = MOCK_TRANSCRIPTS.find((t) => t.id === id);
  const transcript: any = transcriptQuery.data || mock || {
    title: "Transcript",
    date: "",
    duration: "",
    content: "",
    type: "mic",
  };
  const isFailedYoutubeTranscript =
    transcript?.status === "failed" && transcript?.type === "youtube";
  const rawFailureMessage = useMemo(
    () => extractFailureMessage(String(transcript?.content || ""), String(transcript?.step || "")),
    [transcript?.content, transcript?.step],
  );
  const failedMessage = useMemo(
    () => (isFailedYoutubeTranscript ? friendlyRequestMessage(rawFailureMessage, "Transcription failed.") : ""),
    [isFailedYoutubeTranscript, rawFailureMessage],
  );
  const technicalFailureMessage = useMemo(() => {
    if (!isFailedYoutubeTranscript) return "";
    const technical = (rawFailureMessage || "").trim();
    if (!technical || technical === failedMessage) return "";
    return technical;
  }, [failedMessage, isFailedYoutubeTranscript, rawFailureMessage]);
  const failedContentLooksLikeErrorOnly = useMemo(() => {
    if (!isFailedYoutubeTranscript) return false;
    const content = String(transcript?.content || "").trim();
    return /^\[(error|timeout|download error)\]/i.test(content);
  }, [isFailedYoutubeTranscript, transcript?.content]);

  const retryYoutubeTranscription = useCallback(async () => {
    if (!id || isRetryingYoutube) return;
    const sourceUrl = String(transcript?.sourceUrl || "").trim();
    if (!sourceUrl) {
      toast({
        title: "Retry unavailable",
        description: "No source URL is available for this transcript.",
        variant: "destructive",
      });
      return;
    }

    setIsRetryingYoutube(true);
    try {
      const res = await fetch(apiUrl("/api/youtube/transcribe"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          url: sourceUrl,
          title: transcript?.title,
          channelTitle: transcript?.channel,
          thumbnailUrl: transcript?.thumbnailUrl,
          duration: transcript?.duration,
        }),
      });
      if (!res.ok) {
        throw new Error(await responseErrorMessage(res));
      }

      const rec = await res.json();
      if (!rec?.id) {
        throw new Error("Retry started, but no transcript ID was returned.");
      }

      toast({
        title: "Retry started",
        description: "A new YouTube transcription attempt has been queued.",
        duration: 3000,
      });
      queryClient.invalidateQueries({ queryKey: ["/api/transcripts"] });
      setLocation(`/transcript/${rec.id}`);
    } catch (e) {
      toast({
        title: "Retry failed",
        description: friendlyError(e, "Could not restart transcription."),
        variant: "destructive",
        duration: 5000,
      });
    } finally {
      setIsRetryingYoutube(false);
    }
  }, [id, isRetryingYoutube, queryClient, setLocation, toast, transcript?.sourceUrl, transcript?.title, transcript?.channel, transcript?.thumbnailUrl, transcript?.duration]);

  const handleCopyTranscript = () => {
    navigator.clipboard.writeText(transcript?.content || "");
    setCopied(true);
    toast({
      title: "Copied to Clipboard",
      description: "Transcript content has been copied.",
      duration: 2000,
    });
    setTimeout(() => setCopied(false), 2000);
  };

  const [copiedSummary, setCopiedSummary] = useState(false);
  const handleCopySummary = () => {
    navigator.clipboard.writeText(transcript?.summary || "");
    setCopiedSummary(true);
    toast({
      title: "Copied to Clipboard",
      description: "Summary has been copied.",
      duration: 2000,
    });
    setTimeout(() => setCopiedSummary(false), 2000);
  };

  const getBackLink = () => {
    switch (transcript?.type) {
      case "youtube":
        return "/youtube";
      case "file":
        return "/file";
      default:
        return "/";
    }
  };

  // Controlled accordion state - reacts when summary becomes available
  const [accordionValue, setAccordionValue] = useState<string[]>(
    transcript.summary ? ["summary"] : ["transcript"]
  );

  // Update accordion when summary becomes available
  useEffect(() => {
    if (transcript.summary) {
      setAccordionValue(["summary"]);
    }
  }, [transcript.summary]);

  return (
    <div className="min-h-screen bg-background flex flex-col">
      {/* Header Toolbar */}
      <header className="sticky top-0 z-40 backdrop-blur-md border-b border-border/50 h-16 flex items-center justify-between px-4 md:px-8 gap-4" style={{ background: 'var(--neu-bg)' }}>
        <div className="flex items-center gap-4 min-w-0 flex-1">
          <Link href={getBackLink()}>
            <Button variant="ghost" size="icon" className="-ml-2 shrink-0" aria-label="Go back">
              <ArrowLeft className="w-5 h-5 text-muted-foreground" />
            </Button>
          </Link>
          <div className="min-w-0 flex-1">
            <FitText className="font-bold tracking-tight text-foreground" minFontSize={14} maxFontSize={24}>
              {transcript?.title || "Transcript"}
            </FitText>
            <p className="text-xs text-muted-foreground truncate">
              {transcript.date} • <DurationText status={transcript.status} duration={transcript.duration} startedAt={transcript.createdAt} />
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {transcript.content && (
            <Button
              variant={copied ? "default" : "outline"}
              size="sm"
              className="hidden md:flex"
              onClick={handleCopyTranscript}
              type="button"
            >
              {copied ? <Check className="w-4 h-4 mr-2" /> : <Copy className="w-4 h-4 mr-2" />}
              {copied ? "Copied!" : "Copy Transcript"}
            </Button>
          )}
          {transcript.summary && (
            <Button
              variant={copiedSummary ? "default" : "outline"}
              size="sm"
              className="hidden md:flex"
              onClick={handleCopySummary}
              type="button"
            >
              {copiedSummary ? <Check className="w-4 h-4 mr-2" /> : <Copy className="w-4 h-4 mr-2" />}
              {copiedSummary ? "Copied!" : "Copy Summary"}
            </Button>
          )}
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm" className="hidden md:flex data-[state=open]:bg-accent" style={{ transform: 'none' }} type="button">
                <Download className="w-4 h-4 mr-2" /> Export
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                onClick={() => {
                  window.open(apiUrl(`/api/transcripts/${id}/export/pdf`), '_blank');
                }}
              >
                <FileText className="w-4 h-4 mr-2" /> Export as PDF
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => {
                  window.open(apiUrl(`/api/transcripts/${id}/export/docx`), '_blank');
                }}
              >
                <FileText className="w-4 h-4 mr-2" /> Export as DOCX
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>

          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="icon" className="md:hidden" aria-label="Open transcript actions" type="button">
                <Download className="w-4 h-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {transcript.content && (
                <DropdownMenuItem onClick={handleCopyTranscript}>
                  <Copy className="w-4 h-4 mr-2" /> Copy Transcript
                </DropdownMenuItem>
              )}
              {transcript.summary && (
                <DropdownMenuItem onClick={handleCopySummary}>
                  <Copy className="w-4 h-4 mr-2" /> Copy Summary
                </DropdownMenuItem>
              )}
              <DropdownMenuItem
                onClick={() => {
                  window.open(apiUrl(`/api/transcripts/${id}/export/pdf`), "_blank");
                }}
              >
                <FileText className="w-4 h-4 mr-2" /> Export as PDF
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => {
                  window.open(apiUrl(`/api/transcripts/${id}/export/docx`), "_blank");
                }}
              >
                <FileText className="w-4 h-4 mr-2" /> Export as DOCX
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          {transcript.status === "processing" && (
            <StopButton transcriptId={id!} onStop={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })} />
          )}
          {isFailedYoutubeTranscript && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                void retryYoutubeTranscription();
              }}
              disabled={isRetryingYoutube}
              type="button"
            >
              {isRetryingYoutube ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <RotateCcw className="w-3 h-3 mr-1" />}
              {isRetryingYoutube ? "Retrying..." : "Retry"}
            </Button>
          )}
          {transcript.status === "completed" && !transcript.summary && !autoSummarize && (
            <div className="hidden md:block">
              <SummarizeButton transcriptId={id} onComplete={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })} />
            </div>
          )}
          {transcript.status === "completed" && !transcript.summary && !autoSummarize && (
            <div className="md:hidden">
              <SummarizeButton transcriptId={id} onComplete={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })} />
            </div>
          )}
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 overflow-y-auto p-4 md:p-8 md:px-16 lg:px-32">
        <div className="max-w-3xl mx-auto space-y-6">

          {/* Meta Card */}
          <div className="flex flex-wrap gap-2">
            <Badge variant="secondary" className="px-3 py-1 font-normal neu-button"><Calendar className="w-3 h-3 mr-1.5" /> {transcript.date}</Badge>
          </div>

          {transcriptQuery.isError && !mock && (
            <QueryErrorState
              title="Could not load transcript"
              description="Please retry loading this transcript."
              onRetry={() => transcriptQuery.refetch()}
            />
          )}

          {/* Processing Status Banner */}
          {transcript.status === "processing" && (
            <div className="neu-status-well p-4 flex items-center gap-3">
              <Loader2 className="w-5 h-5 animate-spin text-primary shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-foreground truncate">
                  {transcript.step || "Processing..."}
                </p>
                <p className="text-xs text-muted-foreground">
                  Elapsed: <DurationText status={transcript.status} duration={transcript.duration} startedAt={transcript.createdAt} />
                </p>
              </div>
            </div>
          )}

          {isFailedYoutubeTranscript && (
            <div className="space-y-2">
              <QueryErrorState
                title="YouTube transcription failed"
                description={failedMessage || "The transcription failed. Please try again."}
                onRetry={() => {
                  void retryYoutubeTranscription();
                }}
              />
              {technicalFailureMessage && (
                <p className="text-xs text-muted-foreground px-1">
                  Technical details: {technicalFailureMessage}
                </p>
              )}
            </div>
          )}

          {/* Accordion with Summary and Transcript */}
          <Accordion type="multiple" value={accordionValue} onValueChange={setAccordionValue} className="space-y-4">
            {/* Summary Section */}
            <AccordionItem value="summary" className="neu-recording-row overflow-hidden">
              <AccordionTrigger className="px-4 py-3 hover:no-underline">
                <div className="flex items-center gap-2">
                  <Sparkles className="w-4 h-4 text-primary" />
                  <span className="text-base font-semibold tracking-tight">Summary</span>
                  {transcript.step?.includes("Summariz") && (
                    <span className="flex items-center gap-1 text-xs text-muted-foreground ml-2">
                      <Loader2 className="w-3 h-3 animate-spin" />
                      {transcript.step}
                    </span>
                  )}
                </div>
              </AccordionTrigger>
              <AccordionContent className="px-4 pb-4">
                {transcript.summary ? (
                  <div className="prose dark:prose-invert max-w-none">
                    <ReactMarkdown>{transcript.summary}</ReactMarkdown>
                  </div>
                ) : (
                  <p className="text-base text-muted-foreground italic">
                    {transcript.status === "completed"
                      ? "No summary yet. Click 'Summarize' in the header to generate one."
                      : "Summary will be available after transcription completes."}
                  </p>
                )}
              </AccordionContent>
            </AccordionItem>

            {/* Transcript Section */}
            <AccordionItem value="transcript" className="neu-recording-row overflow-hidden">
              <AccordionTrigger className="px-4 py-3 hover:no-underline">
                <div className="flex items-center gap-2">
                  <FileText className="w-4 h-4 text-blue-600" />
                  <span className="text-base font-semibold tracking-tight">Transcript</span>
                </div>
              </AccordionTrigger>
              <AccordionContent className="px-4 pb-4">
                <div className="transcript-content">
                  {transcriptQuery.isLoading ? (
                    "Loading..."
                  ) : transcript.status === "processing" ? (
                    <span className="text-muted-foreground italic"></span>
                  ) : isFailedYoutubeTranscript && failedContentLooksLikeErrorOnly ? (
                    <span className="text-muted-foreground italic">
                      {failedMessage || "No transcript text captured."}
                    </span>
                  ) : transcript.content ? (
                    <SpeakerFormattedText content={transcript.content} />
                  ) : (
                    "No transcript text captured."
                  )}
                </div>
              </AccordionContent>
            </AccordionItem>
          </Accordion>

        </div>
      </main>
    </div>
  );
}
