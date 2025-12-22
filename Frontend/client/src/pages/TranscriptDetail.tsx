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
import { useState, useEffect } from "react";
import { wsUrl, apiUrl } from "@/lib/backend";
import ReactMarkdown from "react-markdown";

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
  const [startTime] = useState(() => Date.now());

  useEffect(() => {
    if (transcript.status !== "processing") {
      return;
    }

    // Update every second
    const interval = setInterval(() => {
      const elapsed = Math.floor((Date.now() - startTime) / 1000);
      setElapsedSeconds(elapsed);
    }, 1000);

    return () => clearInterval(interval);
  }, [transcript.status, startTime]);

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
      <header className="sticky top-0 z-40 bg-background/80 backdrop-blur-md border-b border-border h-16 flex items-center justify-between px-4 md:px-8">
        <div className="flex items-center gap-4">
          <Link href={getBackLink()}>
            <Button variant="ghost" size="icon" className="-ml-2">
              <ArrowLeft className="w-5 h-5 text-muted-foreground" />
            </Button>
          </Link>
          <div>
            <h1 className="font-semibold text-lg leading-tight">{transcript?.title || "Transcript"}</h1>
            <p className="text-xs text-muted-foreground">{transcript.date} • {displayDuration}</p>
          </div>
        </div>

        <div className="flex items-center gap-2">
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
            <Badge variant="secondary" className="px-3 py-1 font-normal"><Calendar className="w-3 h-3 mr-1.5" /> {transcript.date}</Badge>
          </div>

          {/* Accordion with Summary and Transcript */}
          <Accordion type="multiple" value={accordionValue} onValueChange={setAccordionValue} className="space-y-4">
            {/* Summary Section */}
            <AccordionItem value="summary" className="border rounded-lg bg-card">
              <AccordionTrigger className="px-4 py-3 hover:no-underline">
                <div className="flex items-center gap-2">
                  <Sparkles className="w-4 h-4 text-primary" />
                  <span className="font-semibold">Summary</span>
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
                  <div className="prose prose-sm dark:prose-invert max-w-none text-foreground/90 leading-relaxed">
                    <ReactMarkdown>{transcript.summary}</ReactMarkdown>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground italic">
                    {transcript.status === "completed"
                      ? "No summary yet. Click 'Summarize' in the header to generate one."
                      : "Summary will be available after transcription completes."}
                  </p>
                )}
              </AccordionContent>
            </AccordionItem>

            {/* Transcript Section */}
            <AccordionItem value="transcript" className="border rounded-lg bg-card">
              <AccordionTrigger className="px-4 py-3 hover:no-underline">
                <div className="flex items-center gap-2">
                  <FileText className="w-4 h-4 text-blue-600" />
                  <span className="font-semibold">Transcript</span>
                  {transcript.status === "processing" && transcript.step && (
                    <span className="flex items-center gap-1 text-xs text-muted-foreground ml-2">
                      <Loader2 className="w-3 h-3 animate-spin" />
                      {transcript.step}
                    </span>
                  )}
                </div>
              </AccordionTrigger>
              <AccordionContent className="px-4 pb-4">
                <p className="text-foreground/90 leading-relaxed whitespace-pre-wrap">
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
