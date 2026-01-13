import { useParams, Link } from "wouter";
import { ArrowLeft, Share2, Download, Copy, Play, Search, Clock, Calendar, Pencil, Check, Loader2, Sparkles, FileText, Square } from "lucide-react";
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
import { useSharedWebSocket } from "@/contexts/WebSocketContext";
import { useState, useEffect, useRef, useLayoutEffect, useCallback } from "react";
import { wsUrl, apiUrl } from "@/lib/backend";
import ReactMarkdown from "react-markdown";

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
  // Regex to match [Speaker N]: at the start of paragraphs
  const speakerPattern = /\[Speaker (\d+)\]:\s*/g;

  // Check if content has speaker labels
  if (!speakerPattern.test(content)) {
    return <span>{content}</span>;
  }

  // Reset regex
  speakerPattern.lastIndex = 0;

  // Split by speaker labels and create segments
  const segments: { speaker: string; text: string }[] = [];
  let lastIndex = 0;
  let match;

  while ((match = speakerPattern.exec(content)) !== null) {
    // If there's text before this match (shouldn't happen in well-formed content)
    if (match.index > lastIndex && segments.length === 0) {
      segments.push({ speaker: "", text: content.slice(lastIndex, match.index) });
    }

    // Find the end of this segment (next speaker label or end of string)
    const nextMatch = speakerPattern.exec(content);
    const endIndex = nextMatch ? nextMatch.index : content.length;
    speakerPattern.lastIndex = match.index + match[0].length; // Reset to after current match

    segments.push({
      speaker: match[1],
      text: content.slice(match.index + match[0].length, endIndex).trim()
    });

    lastIndex = endIndex;

    // If we found a next match, we need to process it
    if (nextMatch) {
      speakerPattern.lastIndex = nextMatch.index;
    }
  }

  // Simple approach: split by double newline and parse each paragraph
  const paragraphs = content.split(/\n\n+/);

  return (
    <div className="space-y-4">
      {paragraphs.map((para, idx) => {
        const labelMatch = para.match(/^\[Speaker (\d+)\]:\s*([\s\S]*)$/);
        if (labelMatch) {
          const speakerNum = parseInt(labelMatch[1], 10);
          const speakerText = labelMatch[2];
          const colorIdx = (speakerNum - 1) % SPEAKER_COLORS.length;
          const colors = SPEAKER_COLORS[colorIdx];

          return (
            <div key={idx} className="flex flex-col gap-1">
              <span
                className={`inline-flex items-center self-start px-2.5 py-0.5 rounded-full text-xs font-medium border ${colors.bg} ${colors.text} ${colors.border}`}
              >
                Speaker {speakerNum}
              </span>
              <p className="leading-relaxed">{speakerText}</p>
            </div>
          );
        }
        return <p key={idx} className="leading-relaxed">{para}</p>;
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
  const [fontSize, setFontSize] = useState(maxFontSize);

  const calculateFit = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;

    const containerWidth = container.offsetWidth;
    if (containerWidth === 0) return;

    // Create a temporary span to measure text width at max font size
    const measureSpan = document.createElement('span');
    measureSpan.style.cssText = `
      position: absolute;
      visibility: hidden;
      white-space: nowrap;
      font-size: ${maxFontSize}px;
      font-weight: bold;
      font-family: inherit;
      letter-spacing: -0.025em;
    `;
    measureSpan.textContent = children;
    document.body.appendChild(measureSpan);

    const textWidth = measureSpan.offsetWidth;
    document.body.removeChild(measureSpan);

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
    <div ref={containerRef} className="w-full">
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
    <Button size="sm" variant="destructive" onClick={handleStop} disabled={isStopping}>
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
    <Button size="sm" variant="outline" onClick={handleSummarize} disabled={isSummarizing}>
      {isSummarizing ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <Sparkles className="w-3 h-3 mr-1" />}
      {isSummarizing ? "Summarizing..." : "Summarize"}
    </Button>
  );
}

export default function TranscriptDetail() {
  const { id } = useParams();
  const { toast } = useToast();
  const [copied, setCopied] = useState(false);
  const queryClient = useQueryClient();

  const transcriptQuery = useQuery({
    queryKey: ["/api/transcripts", id],
    enabled: !!id,
    refetchInterval: (data: any) => {
      const status = data?.status;
      return status === "processing" || status === "recording" ? 1000 : false;
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

  // Local elapsed time counter for processing transcripts
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const startTimeRef = useRef<number | null>(null);
  const isProcessingRef = useRef(false);

  // Track when processing starts/stops
  useEffect(() => {
    const isNowProcessing = transcript.status === "processing";

    // Transition INTO processing
    if (isNowProcessing && !isProcessingRef.current) {
      startTimeRef.current = Date.now();
      setElapsedSeconds(0);
    }

    // Transition OUT of processing
    if (!isNowProcessing && isProcessingRef.current) {
      startTimeRef.current = null;
    }

    isProcessingRef.current = isNowProcessing;
  }, [transcript.status]);

  // Run timer independently - always active, updates based on startTimeRef
  useEffect(() => {
    const interval = setInterval(() => {
      if (startTimeRef.current && isProcessingRef.current) {
        const elapsed = Math.floor((Date.now() - startTimeRef.current) / 1000);
        setElapsedSeconds(elapsed);
      }
    }, 1000);

    return () => clearInterval(interval);
  }, []); // Empty deps - runs once, uses refs for state

  // Format elapsed time as MM:SS
  const formatElapsed = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  };

  // Show elapsed time when processing, otherwise show actual duration
  const displayDuration = transcript.status === "processing"
    ? formatElapsed(elapsedSeconds)
    : transcript.duration;

  // WebSocket with auto-reconnection for real-time updates
  const handleWsMessage = useCallback((msg: any) => {
    if (msg?.type === "history_updated") {
      // Invalidate this specific transcript query to refresh content
      queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] });
    } else if (msg?.type === "error") {
      toast({
        title: "Error",
        description: msg.message || "An error occurred.",
        variant: "destructive",
        duration: 6000,
      });
    }
  }, [id, queryClient, toast]);

  // PERFORMANCE: Uses singleton WebSocket connection (shared across all pages)
  useSharedWebSocket(handleWsMessage);

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
            <Button variant="ghost" size="icon" className="-ml-2 shrink-0">
              <ArrowLeft className="w-5 h-5 text-muted-foreground" />
            </Button>
          </Link>
          <div className="min-w-0 flex-1">
            <FitText className="font-bold tracking-tight text-foreground" minFontSize={14} maxFontSize={24}>
              {transcript?.title || "Transcript"}
            </FitText>
            <p className="text-xs text-muted-foreground truncate">{transcript.date} • {displayDuration}</p>
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {transcript.content && (
            <Button
              variant={copied ? "default" : "outline"}
              size="sm"
              className="hidden md:flex"
              onClick={handleCopyTranscript}
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
            >
              {copiedSummary ? <Check className="w-4 h-4 mr-2" /> : <Copy className="w-4 h-4 mr-2" />}
              {copiedSummary ? "Copied!" : "Copy Summary"}
            </Button>
          )}
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm" className="hidden md:flex data-[state=open]:bg-accent" style={{ transform: 'none' }}>
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
          {transcript.status === "processing" && (
            <StopButton transcriptId={id!} onStop={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })} />
          )}
          {transcript.status === "completed" && !transcript.summary && !autoSummarize && (
            <SummarizeButton transcriptId={id} onComplete={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })} />
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

          {/* Processing Status Banner */}
          {transcript.status === "processing" && (
            <div className="neu-status-well p-4 flex items-center gap-3">
              <Loader2 className="w-5 h-5 animate-spin text-primary shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-foreground truncate">
                  {transcript.step || "Processing..."}
                </p>
                <p className="text-xs text-muted-foreground">
                  Elapsed: {displayDuration}
                </p>
              </div>
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
