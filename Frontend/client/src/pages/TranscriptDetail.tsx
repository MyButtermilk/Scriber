import { useParams, Link } from "wouter";
import { ArrowLeft, Share2, Download, Copy, Play, Search, Clock, Calendar, Pencil, Check, Loader2, Sparkles, FileText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { MOCK_TRANSCRIPTS } from "@/lib/mockData";
import { useToast } from "@/hooks/use-toast";
import { useState, useEffect, useRef, useLayoutEffect, useCallback } from "react";
import { wsUrl, apiUrl } from "@/lib/backend";
import ReactMarkdown from "react-markdown";

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

  // WebSocket connection for real-time updates
  useEffect(() => {
    const ws = new WebSocket(wsUrl("/ws"));

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg?.type === "history_updated") {
          // Invalidate this specific transcript query to refresh content
          queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] });
        }
      } catch {
        // ignore parse errors
      }
    };

    return () => {
      try {
        ws.close();
      } catch {
        // ignore
      }
    };
  }, [id, queryClient]);

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
          <Button variant="outline" size="sm" className="hidden md:flex">
            <Download className="w-4 h-4 mr-2" /> Export
          </Button>
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
                <p className="transcript-content whitespace-pre-wrap">
                  {transcriptQuery.isLoading ? (
                    "Loading..."
                  ) : transcript.status === "processing" ? (
                    <span className="text-muted-foreground italic"></span>
                  ) : transcript.content ? (
                    transcript.content
                  ) : (
                    "No transcript text captured."
                  )}
                </p>
              </AccordionContent>
            </AccordionItem>
          </Accordion>

        </div>
      </main>
    </div>
  );
}
