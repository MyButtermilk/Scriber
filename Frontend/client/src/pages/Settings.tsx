import { User, CreditCard, Keyboard, Shield, Zap, Globe, ChevronDown, LogOut, Eye, EyeOff, Check, Mic, Mic2, MousePointerClick, ToggleLeft, AudioLines, BarChart3, Power, Key, Settings2, Star, Download, Trash2, Loader2 } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { useState, useEffect, useCallback } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogDescription } from "@/components/ui/dialog";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import { Textarea } from "@/components/ui/textarea";
import { Slider } from "@/components/ui/slider";
import { apiUrl } from "@/lib/backend";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import { useSharedWebSocket } from "@/contexts/WebSocketContext";
import { QueryErrorState } from "@/components/ui/query-error-state";

type OnnxModelInfo = {
  id: string;
  name: string;
  description: string;
  languages: string[];
  sizeMb: number;
  sizeMbByQuantization?: Record<string, number>;
  supportedQuantizations?: string[];
  supportsTimestamps?: boolean;
  downloaded?: boolean;
  status?: "ready" | "not_downloaded" | "downloading" | "error";
  progress?: number;
  message?: string;
};

type NemoModelInfo = {
  id: string;
  name: string;
  description: string;
  languages: string[];
  sizeMb: number;
  supportsTimestamps?: boolean;
  downloaded?: boolean;
  status?: "ready" | "not_downloaded" | "downloading" | "error";
  progress?: number;
  message?: string;
};

const LANGUAGE_OPTIONS = [
  { value: "auto", label: "Auto-detect" },
  { value: "de", label: "German" },
  { value: "en", label: "English" },
  { value: "es", label: "Spanish" },
  { value: "fr", label: "French" },
  { value: "it", label: "Italian" },
] as const;

const TRANSCRIPTION_MODEL_OPTIONS = [
  { value: "onnx_local", label: "Local (ONNX) - No API Key" },
  { value: "nemo_local", label: "Local (NeMo) - Primeline" },
  { value: "soniox-realtime", label: "Soniox Realtime" },
  { value: "soniox-async", label: "Soniox Async" },
  { value: "mistral-realtime", label: "Mistral Realtime (Voxtral RT)" },
  { value: "mistral-async", label: "Mistral Async (Voxtral V2)" },
  { value: "assemblyai", label: "Assembly AI Universal-3-Pro" },
  { value: "deepgram", label: "Deepgram" },
  { value: "openai", label: "OpenAI" },
  { value: "azure", label: "Azure Speech" },
  { value: "gladia", label: "Gladia" },
  { value: "groq", label: "Groq" },
  { value: "speechmatics", label: "Speechmatics" },
  { value: "elevenlabs", label: "ElevenLabs" },
  { value: "google", label: "Google Cloud STT" },
  { value: "aws", label: "AWS Transcribe" },
] as const;

const SUMMARIZATION_MODEL_OPTIONS = [
  { value: "gemini-3-flash-preview", label: "Gemini 3.0 Flash Preview (Recommended)" },
  { value: "gemini-3-pro-preview", label: "Gemini 3 Pro" },
  { value: "gpt-5.2", label: "OpenAI GPT 5.2" },
  { value: "gpt-5-mini", label: "OpenAI GPT 5 Mini" },
  { value: "gpt-5-nano", label: "OpenAI GPT 5 Nano" },
] as const;

function LanguageFlag({ value, className }: { value: string; className?: string }) {
  if (value === "auto") {
    return (
      <span className={cn("language-flag-icon", className)} aria-hidden="true">
        <Globe className="globe-svg" />
      </span>
    );
  }

  if (value === "de") {
    return (
      <span className={cn("language-flag-icon", className)} aria-hidden="true">
        <svg className="rect-flag" viewBox="0 0 3 3" preserveAspectRatio="none">
          <rect width="3" height="1" y="0" fill="#000000" />
          <rect width="3" height="1" y="1" fill="#FF0000" />
          <rect width="3" height="1" y="2" fill="#FFCC00" />
        </svg>
      </span>
    );
  }

  if (value === "en") {
    return (
      <span className={cn("language-flag-icon", className)} aria-hidden="true">
        <svg className="rect-flag" viewBox="0 0 64 64" preserveAspectRatio="none">
          <rect width="64" height="64" fill="#012169" />
          <line x1="0" y1="0" x2="64" y2="64" stroke="#fff" strokeWidth="12" />
          <line x1="0" y1="64" x2="64" y2="0" stroke="#fff" strokeWidth="12" />
          <line x1="0" y1="0" x2="64" y2="64" stroke="#C8102E" strokeWidth="6" />
          <line x1="0" y1="64" x2="64" y2="0" stroke="#C8102E" strokeWidth="6" />
          <line x1="32" y1="0" x2="32" y2="64" stroke="#fff" strokeWidth="16" />
          <line x1="0" y1="32" x2="64" y2="32" stroke="#fff" strokeWidth="16" />
          <line x1="32" y1="0" x2="32" y2="64" stroke="#C8102E" strokeWidth="10" />
          <line x1="0" y1="32" x2="64" y2="32" stroke="#C8102E" strokeWidth="10" />
        </svg>
      </span>
    );
  }

  if (value === "es") {
    return (
      <span className={cn("language-flag-icon", className)} aria-hidden="true">
        <svg className="rect-flag" viewBox="0 0 3 3" preserveAspectRatio="none">
          <rect width="3" height="0.75" y="0" fill="#AA151B" />
          <rect width="3" height="1.5" y="0.75" fill="#F1BF00" />
          <rect width="3" height="0.75" y="2.25" fill="#AA151B" />
          <circle cx="0.8" cy="1.5" r="0.35" fill="#AA151B" />
        </svg>
      </span>
    );
  }

  if (value === "fr") {
    return (
      <span className={cn("language-flag-icon", className)} aria-hidden="true">
        <svg className="rect-flag" viewBox="0 0 3 3" preserveAspectRatio="none">
          <rect width="1" height="3" x="0" fill="#0055A4" />
          <rect width="1" height="3" x="1" fill="#FFFFFF" />
          <rect width="1" height="3" x="2" fill="#EF4135" />
        </svg>
      </span>
    );
  }

  return (
    <span className={cn("language-flag-icon", className)} aria-hidden="true">
      <svg className="rect-flag" viewBox="0 0 3 3" preserveAspectRatio="none">
        <rect width="1" height="3" x="0" fill="#009246" />
        <rect width="1" height="3" x="1" fill="#FFFFFF" />
        <rect width="1" height="3" x="2" fill="#CE2B37" />
      </svg>
    </span>
  );
}

export default function Settings() {
  const [openAIKey, setOpenAIKey] = useState("");
  const [deepgramKey, setDeepgramKey] = useState("");
  const [assemblyAIKey, setAssemblyAIKey] = useState("");
  const [geminiKey, setGeminiKey] = useState("");
  const [youtubeKey, setYoutubeKey] = useState("");
  const [sonioxKey, setSonioxKey] = useState("");
  const [mistralKey, setMistralKey] = useState("");
  const [elevenLabsKey, setElevenLabsKey] = useState("");
  const [azureKey, setAzureKey] = useState("");
  const [azureRegion, setAzureRegion] = useState("");
  const [gladiaKey, setGladiaKey] = useState("");
  const [groqKey, setGroqKey] = useState("");
  const [awsKey, setAwsKey] = useState("");

  const [customVocabulary, setCustomVocabulary] = useState("");
  const [summarizationPrompt, setSummarizationPrompt] = useState("");

  const [showOpenAIKey, setShowOpenAIKey] = useState(false);
  const [showDeepgramKey, setShowDeepgramKey] = useState(false);
  const [showAssemblyAIKey, setShowAssemblyAIKey] = useState(false);
  const [showGeminiKey, setShowGeminiKey] = useState(false);
  const [showYoutubeKey, setShowYoutubeKey] = useState(false);
  const [showSonioxKey, setShowSonioxKey] = useState(false);
  const [showMistralKey, setShowMistralKey] = useState(false);
  const [showElevenLabsKey, setShowElevenLabsKey] = useState(false);
  const [showAzureKey, setShowAzureKey] = useState(false);
  const [showGladiaKey, setShowGladiaKey] = useState(false);
  const [showGroqKey, setShowGroqKey] = useState(false);
  const [showAwsKey, setShowAwsKey] = useState(false);

  const [hotkey, setHotkey] = useState("Ctrl + Shift + S");
  const [recordingMode, setRecordingMode] = useState("press_hold");
  const [isRecordingHotkey, setIsRecordingHotkey] = useState(false);
  const { toast } = useToast();
  const [savedKeys, setSavedKeys] = useState<Record<string, boolean>>({});

  const [inputDevices, setInputDevices] = useState<{ deviceId: string, label: string }[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState("default");
  const [transcriptionModel, setTranscriptionModel] = useState("soniox-realtime");
  const [summarizationModel, setSummarizationModel] = useState("gemini-3-flash-preview");
  const [autoSummarize, setAutoSummarize] = useState(false);
  const [language, setLanguage] = useState("auto");
  const [visualizerBarCount, setVisualizerBarCount] = useState(45);
  const [autostartEnabled, setAutostartEnabled] = useState(false);
  const [autostartAvailable, setAutostartAvailable] = useState(false);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [micAlwaysOn, setMicAlwaysOn] = useState(false);
  const [favoriteMic, setFavoriteMic] = useState("");
  const [isMicDropdownOpen, setIsMicDropdownOpen] = useState(false);
  const [isLanguageDropdownOpen, setIsLanguageDropdownOpen] = useState(false);
  const [isTranscriptionModelDropdownOpen, setIsTranscriptionModelDropdownOpen] = useState(false);
  const [isSummarizationModelDropdownOpen, setIsSummarizationModelDropdownOpen] = useState(false);

  const [onnxAvailable, setOnnxAvailable] = useState<boolean | null>(null);
  const [onnxMessage, setOnnxMessage] = useState("");
  const [onnxModels, setOnnxModels] = useState<OnnxModelInfo[]>([]);
  const [onnxModel, setOnnxModel] = useState("");
  const [onnxQuantization, setOnnxQuantization] = useState("int8");

  const [nemoAvailable, setNemoAvailable] = useState<boolean | null>(null);
  const [nemoMessage, setNemoMessage] = useState("");
  const [nemoModels, setNemoModels] = useState<NemoModelInfo[]>([]);
  const [nemoModel, setNemoModel] = useState("");

  const loadOnnxModels = useCallback(async () => {
    try {
      const res = await fetch(apiUrl("/api/onnx/models"), { credentials: "include" });
      if (!res.ok) {
        throw new Error(await res.text());
      }
      const data = await res.json();
      const available = data.available !== false;
      setOnnxAvailable(available);
      setOnnxMessage(data.message || "");
      const models = (data.models || []) as OnnxModelInfo[];
      setOnnxModels(models);

      const current = data.currentModel || "";
      const selected = models.find((m) => m.id === current) ? current : (models[0]?.id || "");
      setOnnxModel(selected);
      setOnnxQuantization(data.quantization || "int8");
    } catch (e: any) {
      setOnnxAvailable(false);
      setOnnxMessage(String(e?.message || e));
    }
  }, []);

  const loadNemoModels = useCallback(async () => {
    try {
      const res = await fetch(apiUrl("/api/nemo/models"), { credentials: "include" });
      if (!res.ok) {
        throw new Error(await res.text());
      }
      const data = await res.json();
      const available = data.available !== false;
      setNemoAvailable(available);
      setNemoMessage(data.message || "");
      const models = (data.models || []) as NemoModelInfo[];
      setNemoModels(models);

      const current = data.currentModel || "";
      const selected = models.find((m) => m.id === current) ? current : (models[0]?.id || "");
      setNemoModel(selected);
    } catch (e: any) {
      setNemoAvailable(false);
      setNemoMessage(String(e?.message || e));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    const serviceToModel = (service: string, sonioxMode: string) => {
      if (service === "soniox" || service === "soniox_async") {
        return sonioxMode === "async" || service === "soniox_async"
          ? "soniox-async"
          : "soniox-realtime";
      }
      if (service === "mistral" || service === "mistral_async") {
        return service === "mistral_async" ? "mistral-async" : "mistral-realtime";
      }
      return service || "soniox-realtime";
    };

    const load = async () => {
      try {
        setSettingsError("");
        // Load core settings data in parallel.
        const [settingsRes, micsRes, autostartRes] = await Promise.all([
          fetch(apiUrl("/api/settings"), { credentials: "include" }),
          fetch(apiUrl("/api/microphones"), { credentials: "include" }),
          fetch(apiUrl("/api/autostart"), { credentials: "include" }),
        ]);

        if (!settingsRes.ok) throw new Error(await settingsRes.text());
        if (!micsRes.ok) throw new Error(await micsRes.text());

        const settings = await settingsRes.json();
        const mics = await micsRes.json();
        const autostart = autostartRes.ok ? await autostartRes.json() : { enabled: false, available: false };
        if (cancelled) return;

        const keys = settings.apiKeys || {};
        setAutostartEnabled(autostart.enabled || false);
        setAutostartAvailable(autostart.available || false);
        setHotkey(settings.hotkey || settings.hotkeyRaw || "");
        setRecordingMode(settings.mode === "push_to_talk" ? "press_hold" : "start_stop");
        setSelectedDeviceId(settings.micDevice || "default");
        setLanguage(settings.language || "auto");
        setTranscriptionModel(serviceToModel(settings.defaultSttService || "", settings.sonioxMode || "realtime"));
        setCustomVocabulary(settings.customVocab || "");
        setSummarizationPrompt(settings.summarizationPrompt || "");
        setSummarizationModel(settings.summarizationModel || "gemini-3-flash-preview");
        setAutoSummarize(settings.autoSummarize === true);
        setVisualizerBarCount(settings.visualizerBarCount || 45);
        setMicAlwaysOn(settings.micAlwaysOn === true);
        setFavoriteMic(settings.favoriteMic || "");
        setNemoModel(settings.nemoModel || "");

        setSonioxKey(keys.soniox || "");
        setMistralKey(keys.mistral || "");
        setAssemblyAIKey(keys.assemblyai || "");
        setDeepgramKey(keys.deepgram || "");
        setOpenAIKey(keys.openai || "");
        setGeminiKey(keys.googleApiKey || "");
        setYoutubeKey(keys.youtubeApiKey || "");
        setElevenLabsKey(keys.elevenlabs || "");
        setAzureKey(keys.azureSpeechKey || "");
        setAzureRegion(keys.azureSpeechRegion || "");
        setGladiaKey(keys.gladia || "");
        setGroqKey(keys.groq || "");

        setInputDevices((mics.devices || []) as { deviceId: string, label: string }[]);

        // Show page immediately - don't wait for model info
        setSettingsLoaded(true);
      } catch (e: any) {
        setSettingsLoaded(true); // Still mark as loaded even on error
        setSettingsError(String(e?.message || e));
        toast({
          title: "Failed to load settings",
          description: String(e?.message || e),
          duration: 4000,
        });
      }
    };

    load();
    return () => {
      cancelled = true;
    };
  }, [toast]);

  useEffect(() => {
    if (transcriptionModel === "onnx_local" && onnxAvailable === null) {
      loadOnnxModels();
    }
    if (transcriptionModel === "nemo_local" && nemoAvailable === null) {
      loadNemoModels();
    }
  }, [transcriptionModel, onnxAvailable, nemoAvailable, loadOnnxModels, loadNemoModels]);

  const updateSettings = async (patch: any) => {
    const res = await fetch(apiUrl("/api/settings"), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
      credentials: "include",
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || res.statusText);
    }
    return await res.json();
  };

  const refreshMicrophones = useCallback(async () => {
    try {
      const res = await fetch(apiUrl("/api/microphones"), { credentials: "include" });
      if (!res.ok) {
        return;
      }
      const data = await res.json();
      const devices = ((data?.devices || []) as { deviceId: string; label: string }[]);
      setInputDevices(devices);
      const availableIds = new Set(devices.map((d) => d.deviceId));
      setSelectedDeviceId((prev) => {
        if (prev !== "default" && !availableIds.has(prev)) {
          return "default";
        }
        return prev;
      });
    } catch {
      // Best effort refresh on window focus.
    }
  }, []);

  const handleSaveApiKey = async (provider: string) => {
    try {
      if (provider === "AWS") {
        throw new Error("AWS credentials are not managed yet (use standard AWS env vars).");
      }
      const apiKeys: Record<string, string> = {};
      if (provider === "OpenAI") apiKeys.openai = openAIKey;
      if (provider === "Deepgram") apiKeys.deepgram = deepgramKey;
      if (provider === "AssemblyAI") apiKeys.assemblyai = assemblyAIKey;
      if (provider === "Gemini") apiKeys.googleApiKey = geminiKey;
      if (provider === "YouTube") apiKeys.youtubeApiKey = youtubeKey;
      if (provider === "Soniox") apiKeys.soniox = sonioxKey;
      if (provider === "Mistral") apiKeys.mistral = mistralKey;
      if (provider === "ElevenLabs") apiKeys.elevenlabs = elevenLabsKey;
      if (provider === "Azure") {
        apiKeys.azureSpeechKey = azureKey;
        apiKeys.azureSpeechRegion = azureRegion;
      }
      if (provider === "Gladia") apiKeys.gladia = gladiaKey;
      if (provider === "Groq") apiKeys.groq = groqKey;

      await updateSettings({ apiKeys });

      setSavedKeys((prev) => ({ ...prev, [provider]: true }));
      toast({
        title: "Saved",
        description: `${provider} settings updated.`,
        duration: 2000,
      });
      setTimeout(() => {
        setSavedKeys((prev) => ({ ...prev, [provider]: false }));
      }, 2000);
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleHotkeyRecord = (e: React.KeyboardEvent) => {
    e.preventDefault();
    const keys = [];
    if (e.ctrlKey) keys.push("Ctrl");
    if (e.shiftKey) keys.push("Shift");
    if (e.altKey) keys.push("Alt");
    if (e.metaKey) keys.push("Meta");
    if (e.key && !["Control", "Shift", "Alt", "Meta"].includes(e.key)) {
      keys.push(e.key.toUpperCase());
    }

    if (keys.length > 0) {
      setHotkey(keys.join(" + "));
    }
  };

  const handleMicDeviceChange = async (deviceId: string) => {
    setSelectedDeviceId(deviceId);
    try {
      await updateSettings({ micDevice: deviceId });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleMicDeviceSelectFromDropdown = async (deviceId: string) => {
    await handleMicDeviceChange(deviceId);
    window.setTimeout(() => {
      setIsMicDropdownOpen(false);
    }, 500);
  };

  const handleSetFavoriteMic = async (deviceId: string) => {
    // Toggle favorite - if already favorite, clear it
    const originalFavorite = favoriteMic;  // Capture before optimistic update
    const newFavorite = favoriteMic === deviceId ? "" : deviceId;
    setFavoriteMic(newFavorite);
    try {
      await updateSettings({ favoriteMic: newFavorite });
      toast({
        title: newFavorite ? "Favorite set" : "Favorite cleared",
        description: newFavorite
          ? "This microphone will be used automatically when available."
          : "No preferred microphone set.",
        duration: 2000,
      });
    } catch (e: any) {
      setFavoriteMic(originalFavorite); // Revert to original value on error
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleTranscriptionModelChange = async (value: string) => {
    setTranscriptionModel(value);
    try {
      if (value === "soniox-async") {
        await updateSettings({ defaultSttService: "soniox", sonioxMode: "async" });
      } else if (value === "soniox-realtime") {
        await updateSettings({ defaultSttService: "soniox", sonioxMode: "realtime" });
      } else if (value === "mistral-async") {
        await updateSettings({ defaultSttService: "mistral_async" });
      } else if (value === "mistral-realtime") {
        await updateSettings({ defaultSttService: "mistral" });
      } else {
        await updateSettings({ defaultSttService: value });
      }
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleLanguageChange = async (value: string) => {
    setLanguage(value);
    try {
      await updateSettings({ language: value });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleTranscriptionModelSelectFromDropdown = async (value: string) => {
    await handleTranscriptionModelChange(value);
    window.setTimeout(() => {
      setIsTranscriptionModelDropdownOpen(false);
    }, 500);
  };

  const handleLanguageSelectFromDropdown = async (value: string) => {
    await handleLanguageChange(value);
    window.setTimeout(() => {
      setIsLanguageDropdownOpen(false);
    }, 500);
  };

  const handleOnnxModelChange = async (value: string) => {
    setOnnxModel(value);
    try {
      const selected = onnxModels.find((m) => m.id === value);
      const supported = selected?.supportedQuantizations || ["int8", "fp32"];
      if (!supported.includes(onnxQuantization)) {
        const nextQuant = supported[0];
        setOnnxQuantization(nextQuant);
        await updateSettings({ onnxModel: value, onnxQuantization: nextQuant });
      } else {
        await updateSettings({ onnxModel: value });
      }
      toast({
        title: "Saved",
        description: "Local model selection updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleOnnxQuantizationChange = async (value: string) => {
    setOnnxQuantization(value);
    try {
      await updateSettings({ onnxQuantization: value });
      await loadOnnxModels();
      toast({
        title: "Saved",
        description: "Quantization updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleOnnxDownload = async (modelId: string) => {
    if (!modelId) return;
    setOnnxModels((prev) =>
      prev.map((m) =>
        m.id === modelId
          ? { ...m, status: "downloading", progress: 0, message: "Starting download..." }
          : m
      )
    );
    try {
      const res = await fetch(apiUrl("/api/onnx/download"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ modelId, quantization: onnxQuantization }),
        credentials: "include",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data?.success === false) {
        throw new Error(data?.message || "Download failed");
      }
      toast({
        title: "Download finished",
        description: "Model downloaded successfully.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Download failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      await loadOnnxModels();
    }
  };

  const handleOnnxDelete = async (modelId: string) => {
    if (!modelId) return;
    try {
      const res = await fetch(
        apiUrl(`/api/onnx/models/${encodeURIComponent(modelId)}?quantization=${encodeURIComponent(onnxQuantization)}`),
        {
          method: "DELETE",
          credentials: "include",
        }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data?.success === false) {
        throw new Error(data?.message || "Delete failed");
      }
      toast({
        title: "Deleted",
        description: "Model removed from cache.",
        duration: 2000,
      });
      await loadOnnxModels();
    } catch (e: any) {
      toast({
        title: "Delete failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleNemoModelChange = async (value: string) => {
    setNemoModel(value);
    try {
      await updateSettings({ nemoModel: value });
      toast({
        title: "Saved",
        description: "NeMo model updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleNemoDownload = async (modelId: string) => {
    if (!modelId) return;
    setNemoModels((prev) =>
      prev.map((m) =>
        m.id === modelId
          ? { ...m, status: "downloading", progress: 0, message: "Starting download..." }
          : m
      )
    );
    try {
      const res = await fetch(apiUrl("/api/nemo/download"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ modelId }),
        credentials: "include",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data?.success === false) {
        throw new Error(data?.message || "Download failed");
      }
      toast({
        title: "Download finished",
        description: "Model downloaded successfully.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Download failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      await loadNemoModels();
    }
  };

  const handleNemoDelete = async (modelId: string) => {
    if (!modelId) return;
    try {
      const res = await fetch(
        apiUrl(`/api/nemo/models/${encodeURIComponent(modelId)}`),
        {
          method: "DELETE",
          credentials: "include",
        }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data?.success === false) {
        throw new Error(data?.message || "Delete failed");
      }
      toast({
        title: "Deleted",
        description: "Model removed from cache.",
        duration: 2000,
      });
      await loadNemoModels();
    } catch (e: any) {
      toast({
        title: "Delete failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleSummarizationModelChange = async (value: string) => {
    setSummarizationModel(value);
    try {
      await updateSettings({ summarizationModel: value });
      toast({
        title: "Saved",
        description: "Summarization model updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleSummarizationModelSelectFromDropdown = async (value: string) => {
    await handleSummarizationModelChange(value);
    window.setTimeout(() => {
      setIsSummarizationModelDropdownOpen(false);
    }, 500);
  };

  const handleAutoSummarizeChange = async (enabled: boolean) => {
    setAutoSummarize(enabled);
    try {
      await updateSettings({ autoSummarize: enabled });
      toast({
        title: "Saved",
        description: enabled ? "Auto-summarize enabled." : "Auto-summarize disabled.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleSaveHotkey = async () => {
    try {
      const updated = await updateSettings({ hotkey });
      setHotkey(updated.hotkey || hotkey);
      toast({
        title: "Saved",
        description: "Hotkey updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      setIsRecordingHotkey(false);
    }
  };

  const handleRecordingModeChange = async (mode: string) => {
    setRecordingMode(mode);
    try {
      await updateSettings({ mode: mode === "press_hold" ? "push_to_talk" : "toggle" });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleCustomVocabBlur = async () => {
    try {
      await updateSettings({ customVocab: customVocabulary });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleSummarizationPromptBlur = async () => {
    try {
      await updateSettings({ summarizationPrompt });
      toast({
        title: "Saved",
        description: "Summarization prompt updated.",
        duration: 2000,
      });
    } catch (e: any) {
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleVisualizerBarCountChange = async (value: number[]) => {
    const count = value[0];
    const prevCount = visualizerBarCount;
    setVisualizerBarCount(count);
    try {
      await updateSettings({ visualizerBarCount: count });
    } catch (e: any) {
      setVisualizerBarCount(prevCount);
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleAutostartChange = async (enabled: boolean) => {
    setAutostartEnabled(enabled);
    try {
      const res = await fetch(apiUrl("/api/autostart"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
        credentials: "include",
      });

      if (!res.ok) {
        const error = await res.json();
        throw new Error(error.message || "Failed to update autostart");
      }

      toast({
        title: "Saved",
        description: enabled ? "Autostart enabled" : "Autostart disabled",
        duration: 2000,
      });
    } catch (e: any) {
      // Revert on error
      setAutostartEnabled(!enabled);
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleMicAlwaysOnChange = async (enabled: boolean) => {
    setMicAlwaysOn(enabled);
    try {
      await updateSettings({ micAlwaysOn: enabled });
      toast({
        title: "Saved",
        description: enabled ? "Mic pre-warming enabled" : "Mic pre-warming disabled",
        duration: 2000,
      });
    } catch (e: any) {
      setMicAlwaysOn(!enabled);
      toast({
        title: "Save failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const handleWsMessage = useCallback((msg: any) => {
    if (!msg) return;
    if (msg.type === "microphones_updated") {
      const devices = ((msg.devices || []) as { deviceId: string; label: string }[]);
      setInputDevices(devices);

      const availableIds = new Set(devices.map((d) => d.deviceId));
      if (selectedDeviceId !== "default" && !availableIds.has(selectedDeviceId)) {
        setSelectedDeviceId("default");
        toast({
          title: "Mikrofon getrennt",
          description: "Das ausgewahlte Mikrofon ist nicht mehr verfugbar. Es wurde auf Default zuruckgestellt.",
          duration: 3000,
        });
      }

      if (msg.favoriteMicRestored && typeof msg.restoredDeviceId === "string" && msg.restoredDeviceId) {
        setSelectedDeviceId(msg.restoredDeviceId);
        setFavoriteMic(msg.restoredDeviceId);
        const restoredLabel =
          typeof msg.restoredDeviceLabel === "string" && msg.restoredDeviceLabel
            ? msg.restoredDeviceLabel
            : msg.restoredDeviceId;
        toast({
          title: "Favorite mic restored",
          description: `Favorite microphone '${restoredLabel}' is available again.`,
          duration: 2500,
        });
      }
      return;
    }
    if (msg.type === "onnx_download_progress") {
      setOnnxModels((prev) =>
        prev.map((m) =>
          m.id === msg.modelId
            ? {
              ...m,
              status: msg.status,
              progress: typeof msg.progress === "number" ? msg.progress : m.progress,
              message: msg.message || m.message,
              downloaded: msg.status === "ready" ? true : m.downloaded,
            }
            : m
        )
      );
    }
    if (msg.type === "onnx_models_updated") {
      loadOnnxModels();
    }
    if (msg.type === "nemo_download_progress") {
      setNemoModels((prev) =>
        prev.map((m) =>
          m.id === msg.modelId
            ? {
              ...m,
              status: msg.status,
              progress: typeof msg.progress === "number" ? msg.progress : m.progress,
              message: msg.message || m.message,
              downloaded: msg.status === "ready" ? true : m.downloaded,
            }
            : m
        )
      );
    }
    if (msg.type === "nemo_models_updated") {
      loadNemoModels();
    }
  }, [loadOnnxModels, loadNemoModels, selectedDeviceId, toast]);

  useSharedWebSocket(handleWsMessage);

  useEffect(() => {
    const onWindowFocus = () => {
      void refreshMicrophones();
    };
    window.addEventListener("focus", onWindowFocus);
    return () => {
      window.removeEventListener("focus", onWindowFocus);
    };
  }, [refreshMicrophones]);

  const selectedOnnxModel = onnxModels.find((m) => m.id === onnxModel) || onnxModels[0];
  const selectedNemoModel = nemoModels.find((m) => m.id === nemoModel) || nemoModels[0];
  const selectedMicDevice = inputDevices.find(
    (device, index) => (device.deviceId || `device-${index}`) === selectedDeviceId
  );
  const selectedMicLabel = inputDevices.length === 0
    ? "Loading devices..."
    : (selectedMicDevice?.label || (selectedDeviceId === "default" ? "Default" : ""));
  const hasSelectedMic = Boolean(selectedMicDevice || selectedDeviceId === "default");
  const selectedLanguage = LANGUAGE_OPTIONS.find((option) => option.value === language) || LANGUAGE_OPTIONS[0];
  const selectedTranscriptionModelOption = TRANSCRIPTION_MODEL_OPTIONS.find((option) => option.value === transcriptionModel);
  const selectedSummarizationModelOption = SUMMARIZATION_MODEL_OPTIONS.find((option) => option.value === summarizationModel);
  const supportedQuantizations = selectedOnnxModel?.supportedQuantizations || ["int8", "fp32"];
  const quantizationSupported = supportedQuantizations.includes(onnxQuantization);
  const formatSize = (sizeMb?: number) => {
    if (!sizeMb) return "";
    if (sizeMb >= 1024) return `${(sizeMb / 1024).toFixed(1)} GB`;
    return `${sizeMb} MB`;
  };
  const getStatusLabel = (status?: string) => {
    if (status === "ready") return "Downloaded";
    if (status === "downloading") return "Downloading";
    if (status === "error") return "Error";
    return "Not downloaded";
  };
  const getStatusVariant = (status?: string): "default" | "secondary" | "destructive" | "outline" => {
    if (status === "ready") return "default";
    if (status === "downloading") return "secondary";
    if (status === "error") return "destructive";
    return "outline";
  };

  return (
    <div className={`max-w-screen-md mx-auto px-4 py-6 md:py-8 transition-opacity duration-150 ${settingsLoaded ? 'opacity-100' : 'opacity-0'}`}>
      {settingsError && (
        <QueryErrorState
          className="mb-4"
          title="Could not load settings"
          description={settingsError}
          onRetry={() => window.location.reload()}
        />
      )}
      <header className="mb-6 space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-foreground">Settings</h1>
        <p className="text-muted-foreground">Manage your preferences and API keys</p>
      </header>
      <Accordion type="multiple" defaultValue={["transcription", "api-keys"]} className="space-y-4">

        {/* Transcription Settings */}
        <AccordionItem value="transcription" className="border-0">
          <div className="neu-panel-raised bg-card rounded-xl overflow-hidden">
            <AccordionTrigger className="px-6 py-4 hover:no-underline">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-primary/10 text-primary">
                  <Settings2 className="w-5 h-5" />
                </div>
                <div className="text-left">
                  <h2 className="font-semibold text-foreground">Transcription Settings</h2>
                  <p className="text-sm text-muted-foreground">Recording, language, and model preferences</p>
                </div>
              </div>
            </AccordionTrigger>
            <AccordionContent>
              <div className="px-6 pb-6 space-y-6">

                {autostartAvailable && (
                  <>
                    <div className="flex items-center justify-between">
                      <div className="space-y-0.5">
                        <Label className="text-base">Autostart with Windows</Label>
                        <p className="text-sm text-muted-foreground">Launch Scriber automatically when you log in</p>
                      </div>
                      <Switch
                        checked={autostartEnabled}
                        onCheckedChange={handleAutostartChange}
                      />
                    </div>

                    <Separator />
                  </>
                )}

                <div className="space-y-3">
                  <div className="space-y-0.5">
                    <Label className="text-base">Input Device</Label>
                    <p className="text-sm text-muted-foreground">
                      Select microphone for recording. Star a device to always use it when available.
                    </p>
                  </div>
                  <div className={cn("mic-device-dropdown", isMicDropdownOpen && "is-open")}>
                    <button
                      type="button"
                      className="mic-device-dropdown-header"
                      onClick={() => setIsMicDropdownOpen((prev) => !prev)}
                      aria-label="Select input device"
                      aria-expanded={isMicDropdownOpen}
                      aria-controls="mic-device-dropdown-tray"
                    >
                      <span className="mic-device-dropdown-header-info">
                        <span className={cn("mic-device-dropdown-selected-text", hasSelectedMic && "is-selected")}>
                          {selectedMicLabel || "Select a device..."}
                        </span>
                      </span>
                      <ChevronDown className="mic-device-dropdown-chevron" />
                    </button>

                    <div
                      id="mic-device-dropdown-tray"
                      className="mic-device-dropdown-tray"
                      aria-hidden={!isMicDropdownOpen}
                    >
                      <div className="mic-device-dropdown-content">
                        <div className="mic-device-dropdown-tray-inner">
                          <div className="mic-device-list">
                            {inputDevices.length === 0 ? (
                              <div className="text-sm text-muted-foreground py-2 px-2">Loading devices...</div>
                            ) : (
                              inputDevices.map((device, index) => {
                                const deviceValue = device.deviceId || `device-${index}`;
                                const deviceLabel = device.label || `Device ${index + 1}`;
                                const micInputId = `mic-device-${index}`;
                                const favoriteInputId = `favorite-mic-${index}`;
                                const isSelected = selectedDeviceId === deviceValue;
                                const isFavorite = favoriteMic === deviceValue;
                                return (
                                  <div
                                    key={`${deviceValue}-${index}`}
                                    className={cn(
                                      "mic-device-item",
                                      isSelected && "is-selected",
                                      isFavorite && "is-favorite"
                                    )}
                                  >
                                    <div className="mic-device-row-waves" aria-hidden="true">
                                      <div className="mic-device-wave-row" />
                                    </div>

                                    <input
                                      type="radio"
                                      id={micInputId}
                                      name="mic-input-device"
                                      className="mic-device-radio sr-only"
                                      checked={isSelected}
                                      onChange={() => handleMicDeviceSelectFromDropdown(deviceValue)}
                                      aria-label={`Select microphone ${deviceLabel}`}
                                    />
                                    <label htmlFor={micInputId} className="mic-device-label">
                                      <span className="mic-device-icon-wrapper" aria-hidden="true">
                                        <AudioLines className="mic-device-icon" />
                                      </span>
                                      <span className="mic-device-name">{deviceLabel}</span>
                                      <svg className="mic-device-check" viewBox="0 0 24 24" aria-hidden="true">
                                        <path d="M 4 12 L 10 18 L 20 6" />
                                      </svg>
                                    </label>

                                    <div className="mic-device-divider" aria-hidden="true" />

                                    <input
                                      type="checkbox"
                                      id={favoriteInputId}
                                      className="mic-device-star-radio sr-only"
                                      checked={isFavorite}
                                      onChange={() => handleSetFavoriteMic(deviceValue)}
                                      aria-label={isFavorite ? `Remove ${deviceLabel} from favorites` : `Set ${deviceLabel} as favorite`}
                                    />
                                    <label
                                      htmlFor={favoriteInputId}
                                      className="mic-device-star-label"
                                      title={isFavorite ? "Remove from favorites" : "Set as favorite"}
                                    >
                                      <svg className="mic-device-star" viewBox="0 0 24 24" aria-hidden="true">
                                        <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
                                      </svg>
                                    </label>
                                  </div>
                                );
                              })
                            )}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                  {favoriteMic && (
                    <p className="text-xs text-amber-600 dark:text-amber-400 flex items-center gap-1.5">
                      <Star className="w-3 h-3 fill-current" />
                      Favorite mic will be used automatically when connected
                    </p>
                  )}
                </div>

                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label className="text-base">Mic Pre-warming</Label>
                    <p className="text-sm text-muted-foreground">Keep microphone in standby for instant recording start</p>
                  </div>
                  <Switch
                    checked={micAlwaysOn}
                    onCheckedChange={handleMicAlwaysOnChange}
                  />
                </div>

                <Separator />

                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label className="text-base">Transcription Model</Label>
                    <p className="text-sm text-muted-foreground">Select the AI model for live transcription</p>
                  </div>
                  <div className="space-y-2">
                    <div className={cn("model-dropdown w-[320px]", isTranscriptionModelDropdownOpen && "is-open")}>
                      <button
                        type="button"
                        className="model-dropdown-header"
                        onClick={() => setIsTranscriptionModelDropdownOpen((prev) => !prev)}
                        aria-label="Select live transcription model"
                        aria-expanded={isTranscriptionModelDropdownOpen}
                        aria-controls="transcription-model-dropdown-tray"
                      >
                        <span className="model-dropdown-header-info">
                          <span className="model-dropdown-selected-text is-selected">
                            {selectedTranscriptionModelOption?.label || transcriptionModel || "Select model..."}
                          </span>
                        </span>
                        <ChevronDown className="model-dropdown-chevron" />
                      </button>

                      <div
                        id="transcription-model-dropdown-tray"
                        className="model-dropdown-tray"
                        aria-hidden={!isTranscriptionModelDropdownOpen}
                      >
                        <div className="model-dropdown-content">
                          <div className="model-dropdown-tray-inner">
                            <div className="model-list">
                              {TRANSCRIPTION_MODEL_OPTIONS.map((option) => {
                                const isSelected = option.value === transcriptionModel;
                                const inputId = `transcription-model-option-${option.value}`;
                                return (
                                  <div
                                    key={option.value}
                                    className={cn("model-item", isSelected && "is-selected")}
                                  >
                                    <div className="model-row-waves" aria-hidden="true">
                                      <div className="model-wave-row" />
                                    </div>

                                    <input
                                      type="radio"
                                      id={inputId}
                                      name="transcription-model-option"
                                      className="model-radio sr-only"
                                      checked={isSelected}
                                      onChange={() => handleTranscriptionModelSelectFromDropdown(option.value)}
                                      aria-label={`Select ${option.label} as transcription model`}
                                    />
                                    <label htmlFor={inputId} className="model-option-label">
                                      <span className="model-name">{option.label}</span>
                                      <svg className="model-check" viewBox="0 0 24 24" aria-hidden="true">
                                        <path d="M 4 12 L 10 18 L 20 6" />
                                      </svg>
                                    </label>
                                  </div>
                                );
                              })}
                            </div>
                          </div>
                        </div>
                      </div>
                    </div>
                    {transcriptionModel === "assemblyai" && (
                      <p className="text-xs text-muted-foreground">
                        Async mode: live transcript appears after you stop recording.
                      </p>
                    )}
                  </div>
                </div>

                <Separator />

                <div className="space-y-3">
                  <div className="space-y-0.5">
                    <Label className="text-base">Local Models</Label>
                    <p className="text-sm text-muted-foreground">Run speech recognition locally without API keys</p>
                  </div>

                  {transcriptionModel !== "onnx_local" && (
                    <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                      Select "Local (ONNX) - No API Key" above to load ONNX models.
                    </div>
                  )}

                  {transcriptionModel === "onnx_local" && onnxAvailable === null && (
                    <div className="text-sm text-muted-foreground">Loading local models...</div>
                  )}

                  {transcriptionModel === "onnx_local" && onnxAvailable === false && (
                    <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                      {onnxMessage || "onnx-asr not installed. Run: pip install onnx-asr[cpu,hub]"}
                    </div>
                  )}

                  {transcriptionModel === "onnx_local" && onnxAvailable && (
                    <div className="space-y-4 rounded-lg border border-border/60 bg-secondary/20 p-4">
                      <div className="grid gap-3 md:grid-cols-2">
                        <div className="space-y-1.5">
                          <Label className="text-sm">Model</Label>
                          <Select value={onnxModel} onValueChange={handleOnnxModelChange}>
                            <SelectTrigger>
                              <SelectValue placeholder="Select local model" />
                            </SelectTrigger>
                            <SelectContent>
                              {onnxModels.map((model) => (
                                <SelectItem key={model.id} value={model.id}>{model.name}</SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>

                        <div className="space-y-1.5">
                          <Label className="text-sm">Quantization</Label>
                          <Select value={onnxQuantization} onValueChange={handleOnnxQuantizationChange}>
                            <SelectTrigger>
                              <SelectValue placeholder="Select quantization" />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="int8" disabled={!supportedQuantizations.includes("int8")}>int8 (fast)</SelectItem>
                              <SelectItem value="fp16" disabled={!supportedQuantizations.includes("fp16")}>fp16 (balanced)</SelectItem>
                              <SelectItem value="fp32" disabled={!supportedQuantizations.includes("fp32")}>fp32 (accurate)</SelectItem>
                            </SelectContent>
                          </Select>
                        </div>
                      </div>

                      {selectedOnnxModel ? (
                        <div className="space-y-2">
                          <div className="flex items-center justify-between gap-2">
                            <div className="text-sm font-medium">{selectedOnnxModel.name}</div>
                            <Badge variant={getStatusVariant(selectedOnnxModel.status)}>
                              {getStatusLabel(selectedOnnxModel.status)}
                            </Badge>
                          </div>
                          <p className="text-sm text-muted-foreground">{selectedOnnxModel.description}</p>
                          <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
                            {(() => {
                              const sizeForQuant =
                                selectedOnnxModel.sizeMbByQuantization?.[onnxQuantization] ??
                                selectedOnnxModel.sizeMb;
                              return sizeForQuant ? (
                                <span>Size ({onnxQuantization}): {formatSize(sizeForQuant)}</span>
                              ) : null;
                            })()}
                            {selectedOnnxModel.languages?.length ? (
                              <span>Languages: {selectedOnnxModel.languages.join(", ")}</span>
                            ) : null}
                          </div>

                          {!quantizationSupported && (
                            <div className="text-xs text-destructive">
                              Quantization "{onnxQuantization}" is not supported for this model.
                            </div>
                          )}

                          {selectedOnnxModel.status === "downloading" && (
                            <div className="space-y-2">
                              <Progress value={selectedOnnxModel.progress || 0} />
                              <div className="text-xs text-muted-foreground flex items-center gap-2">
                                <Loader2 className="w-3 h-3 animate-spin" />
                                <span>{selectedOnnxModel.message || "Downloading..."}</span>
                                <span className="ml-auto">{Math.round(selectedOnnxModel.progress || 0)}%</span>
                              </div>
                            </div>
                          )}

                          {selectedOnnxModel.status === "error" && selectedOnnxModel.message && (
                            <div className="text-xs text-destructive">{selectedOnnxModel.message}</div>
                          )}

                          <div className="flex items-center gap-2">
                            <Button
                              size="sm"
                              onClick={() => handleOnnxDownload(selectedOnnxModel.id)}
                              disabled={
                                selectedOnnxModel.status === "downloading" ||
                                selectedOnnxModel.downloaded ||
                                !quantizationSupported
                              }
                            >
                              {selectedOnnxModel.status === "downloading" ? (
                                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                              ) : (
                                <Download className="w-4 h-4 mr-2" />
                              )}
                              Download
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => handleOnnxDelete(selectedOnnxModel.id)}
                              disabled={!selectedOnnxModel.downloaded || selectedOnnxModel.status === "downloading"}
                            >
                              <Trash2 className="w-4 h-4 mr-2" />
                              Delete
                            </Button>
                          </div>
                        </div>
                      ) : (
                        <div className="text-sm text-muted-foreground">No local models available.</div>
                      )}
                    </div>
                  )}
                </div>

                <Separator />

                <div className="space-y-3">
                  <div className="space-y-0.5">
                    <Label className="text-base">NeMo Local Models</Label>
                    <p className="text-sm text-muted-foreground">
                      Run .nemo models locally (requires NeMo toolkit)
                    </p>
                  </div>

                  {transcriptionModel !== "nemo_local" && (
                    <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                      Select "Local (NeMo) - Primeline" above to load NeMo models.
                    </div>
                  )}

                  {transcriptionModel === "nemo_local" && nemoAvailable === null && (
                    <div className="text-sm text-muted-foreground">Loading NeMo models...</div>
                  )}

                  {transcriptionModel === "nemo_local" && nemoAvailable === false && (
                    <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                      {nemoMessage || "NeMo toolkit not installed. Run: pip install nemo_toolkit[asr]"}
                    </div>
                  )}

                  {transcriptionModel === "nemo_local" && nemoAvailable && (
                    <div className="space-y-4 rounded-lg border border-border/60 bg-secondary/20 p-4">
                      <div className="grid gap-3 md:grid-cols-2">
                        <div className="space-y-1.5">
                          <Label className="text-sm">Model</Label>
                          <Select value={nemoModel} onValueChange={handleNemoModelChange}>
                            <SelectTrigger>
                              <SelectValue placeholder="Select NeMo model" />
                            </SelectTrigger>
                            <SelectContent>
                              {nemoModels.map((model) => (
                                <SelectItem key={model.id} value={model.id}>{model.name}</SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      </div>

                      {selectedNemoModel ? (
                        <div className="space-y-2">
                          <div className="flex items-center justify-between gap-2">
                            <div className="text-sm font-medium">{selectedNemoModel.name}</div>
                            <Badge variant={getStatusVariant(selectedNemoModel.status)}>
                              {getStatusLabel(selectedNemoModel.status)}
                            </Badge>
                          </div>
                          <p className="text-sm text-muted-foreground">{selectedNemoModel.description}</p>
                          <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
                            {selectedNemoModel.sizeMb ? (
                              <span>Size: {formatSize(selectedNemoModel.sizeMb)}</span>
                            ) : null}
                            {selectedNemoModel.languages?.length ? (
                              <span>Languages: {selectedNemoModel.languages.join(", ")}</span>
                            ) : null}
                          </div>

                          {selectedNemoModel.status === "downloading" && (
                            <div className="space-y-2">
                              <Progress value={selectedNemoModel.progress || 0} />
                              <div className="text-xs text-muted-foreground flex items-center gap-2">
                                <Loader2 className="w-3 h-3 animate-spin" />
                                <span>{selectedNemoModel.message || "Downloading..."}</span>
                                <span className="ml-auto">{Math.round(selectedNemoModel.progress || 0)}%</span>
                              </div>
                            </div>
                          )}

                          {selectedNemoModel.status === "error" && selectedNemoModel.message && (
                            <div className="text-xs text-destructive">{selectedNemoModel.message}</div>
                          )}

                          <div className="flex items-center gap-2">
                            <Button
                              size="sm"
                              onClick={() => handleNemoDownload(selectedNemoModel.id)}
                              disabled={selectedNemoModel.status === "downloading" || selectedNemoModel.downloaded}
                            >
                              {selectedNemoModel.status === "downloading" ? (
                                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                              ) : (
                                <Download className="w-4 h-4 mr-2" />
                              )}
                              Download
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => handleNemoDelete(selectedNemoModel.id)}
                              disabled={!selectedNemoModel.downloaded || selectedNemoModel.status === "downloading"}
                            >
                              <Trash2 className="w-4 h-4 mr-2" />
                              Delete
                            </Button>
                          </div>
                        </div>
                      ) : (
                        <div className="text-sm text-muted-foreground">No NeMo models available.</div>
                      )}
                    </div>
                  )}
                </div>

                <Separator />

                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label className="text-base">Default Language</Label>
                    <p className="text-sm text-muted-foreground">Fallback language for detection</p>
                  </div>
                  <div className={cn("language-dropdown w-[320px]", isLanguageDropdownOpen && "is-open")}>
                    <button
                      type="button"
                      className="language-dropdown-header"
                      onClick={() => setIsLanguageDropdownOpen((prev) => !prev)}
                      aria-label="Select default transcription language"
                      aria-expanded={isLanguageDropdownOpen}
                      aria-controls="language-dropdown-tray"
                    >
                      <span className="language-dropdown-header-info">
                        <span className="language-dropdown-selected-value-wrapper">
                          <LanguageFlag value={selectedLanguage.value} className="language-header-flag" />
                          <span className="language-dropdown-selected-text is-selected">{selectedLanguage.label}</span>
                        </span>
                      </span>
                      <ChevronDown className="language-dropdown-chevron" />
                    </button>

                    <div
                      id="language-dropdown-tray"
                      className="language-dropdown-tray"
                      aria-hidden={!isLanguageDropdownOpen}
                    >
                      <div className="language-dropdown-content">
                        <div className="language-dropdown-tray-inner">
                          <div className="language-list">
                            {LANGUAGE_OPTIONS.map((option) => {
                              const isSelected = option.value === language;
                              const inputId = `lang-option-${option.value}`;
                              return (
                                <div
                                  key={option.value}
                                  className={cn("language-item", isSelected && "is-selected")}
                                >
                                  <div className="language-row-waves" aria-hidden="true">
                                    <div className="language-wave-row" />
                                  </div>

                                  <input
                                    type="radio"
                                    id={inputId}
                                    name="default-transcription-language"
                                    className="language-radio sr-only"
                                    checked={isSelected}
                                    onChange={() => handleLanguageSelectFromDropdown(option.value)}
                                    aria-label={`Select ${option.label} as default transcription language`}
                                  />
                                  <label htmlFor={inputId} className="language-option-label">
                                    <LanguageFlag value={option.value} />
                                    <span className="language-name">{option.label}</span>
                                    <svg className="language-check" viewBox="0 0 24 24" aria-hidden="true">
                                      <path d="M 4 12 L 10 18 L 20 6" />
                                    </svg>
                                  </label>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

                <Separator />

                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label className="text-base">Summarization Model</Label>
                    <p className="text-sm text-muted-foreground">Select model for summarizing transcripts</p>
                  </div>
                  <div className={cn("model-dropdown w-[320px]", isSummarizationModelDropdownOpen && "is-open")}>
                    <button
                      type="button"
                      className="model-dropdown-header"
                      onClick={() => setIsSummarizationModelDropdownOpen((prev) => !prev)}
                      aria-label="Select summarization model"
                      aria-expanded={isSummarizationModelDropdownOpen}
                      aria-controls="summarization-model-dropdown-tray"
                    >
                      <span className="model-dropdown-header-info">
                        <span className="model-dropdown-selected-text is-selected">
                          {selectedSummarizationModelOption?.label || summarizationModel || "Select model..."}
                        </span>
                      </span>
                      <ChevronDown className="model-dropdown-chevron" />
                    </button>

                    <div
                      id="summarization-model-dropdown-tray"
                      className="model-dropdown-tray"
                      aria-hidden={!isSummarizationModelDropdownOpen}
                    >
                      <div className="model-dropdown-content">
                        <div className="model-dropdown-tray-inner">
                          <div className="model-list">
                            {SUMMARIZATION_MODEL_OPTIONS.map((option) => {
                              const isSelected = option.value === summarizationModel;
                              const inputId = `summarization-model-option-${option.value}`;
                              return (
                                <div
                                  key={option.value}
                                  className={cn("model-item", isSelected && "is-selected")}
                                >
                                  <div className="model-row-waves" aria-hidden="true">
                                    <div className="model-wave-row" />
                                  </div>

                                  <input
                                    type="radio"
                                    id={inputId}
                                    name="summarization-model-option"
                                    className="model-radio sr-only"
                                    checked={isSelected}
                                    onChange={() => handleSummarizationModelSelectFromDropdown(option.value)}
                                    aria-label={`Select ${option.label} as summarization model`}
                                  />
                                  <label htmlFor={inputId} className="model-option-label">
                                    <span className="model-name">{option.label}</span>
                                    <svg className="model-check" viewBox="0 0 24 24" aria-hidden="true">
                                      <path d="M 4 12 L 10 18 L 20 6" />
                                    </svg>
                                  </label>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label className="text-base">Auto-Summarize</Label>
                    <p className="text-sm text-muted-foreground">Automatically summarize transcripts when completed</p>
                  </div>
                  <Switch
                    checked={autoSummarize}
                    onCheckedChange={handleAutoSummarizeChange}
                  />
                </div>


                <Separator />

                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label className="text-base">Global Hotkey</Label>
                    <p className="text-sm text-muted-foreground">Shortcut to start/stop recording</p>
                  </div>

                  <Dialog open={isRecordingHotkey} onOpenChange={setIsRecordingHotkey}>
                    <DialogTrigger asChild>
                      <Button variant="outline" className="min-w-[140px] font-mono">
                        <Keyboard className="w-4 h-4 mr-2 text-muted-foreground" />
                        {hotkey}
                      </Button>
                    </DialogTrigger>
                    <DialogContent className="sm:max-w-[425px]">
                      <DialogHeader>
                        <DialogTitle>Record Hotkey</DialogTitle>
                        <DialogDescription>
                          Press the combination of keys you want to use as a shortcut.
                        </DialogDescription>
                      </DialogHeader>
                      <div
                        className="flex items-center justify-center h-32 border-2 border-dashed rounded-lg bg-secondary/20 outline-none focus:border-primary focus:bg-primary/5 transition-colors"
                        tabIndex={0}
                        onKeyDown={handleHotkeyRecord}
                      >
                        <p className="text-lg font-medium text-primary animate-pulse">Press keys now...</p>
                      </div>
                      <div className="flex justify-end gap-2">
                        <Button variant="ghost" onClick={() => setIsRecordingHotkey(false)}>Cancel</Button>
                        <Button onClick={handleSaveHotkey}>Save</Button>
                      </div>
                    </DialogContent>
                  </Dialog>

                </div>

                <Separator />

                <div className="flex flex-col gap-4">
                  <div className="space-y-0.5">
                    <Label className="text-base">Recording Mode</Label>
                    <p className="text-sm text-muted-foreground">Choose how the hotkey behaves</p>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div
                      className={cn(
                        "flex flex-col gap-2 p-4 rounded-xl border-2 cursor-pointer transition-all hover:bg-accent/5",
                        recordingMode === "press_hold"
                          ? "border-primary bg-primary/5"
                          : "border-border/60 hover:border-primary/50"
                      )}
                      onClick={() => handleRecordingModeChange("press_hold")}
                    >
                      <div className="flex items-center gap-3">
                        <div className={cn(
                          "p-2 rounded-lg",
                          recordingMode === "press_hold" ? "bg-primary/10 text-primary" : "bg-secondary text-muted-foreground"
                        )}>
                          <Mic className="w-5 h-5" />
                        </div>
                        <div className="space-y-1">
                          <p className="font-medium text-sm leading-none">Push and Hold</p>
                          <p className="text-xs text-muted-foreground">Hold key to record, release to stop</p>
                        </div>
                        {recordingMode === "press_hold" && <Check className="w-4 h-4 text-primary ml-auto" />}
                      </div>
                    </div>

                    <div
                      className={cn(
                        "flex flex-col gap-2 p-4 rounded-xl border-2 cursor-pointer transition-all hover:bg-accent/5",
                        recordingMode === "start_stop"
                          ? "border-primary bg-primary/5"
                          : "border-border/60 hover:border-primary/50"
                      )}
                      onClick={() => handleRecordingModeChange("start_stop")}
                    >
                      <div className="flex items-center gap-3">
                        <div className={cn(
                          "p-2 rounded-lg",
                          recordingMode === "start_stop" ? "bg-primary/10 text-primary" : "bg-secondary text-muted-foreground"
                        )}>
                          <ToggleLeft className="w-5 h-5" />
                        </div>
                        <div className="space-y-1">
                          <p className="font-medium text-sm leading-none">Start / Stop</p>
                          <p className="text-xs text-muted-foreground">Press once to start, again to stop</p>
                        </div>
                        {recordingMode === "start_stop" && <Check className="w-4 h-4 text-primary ml-auto" />}
                      </div>
                    </div>
                  </div>
                </div>

                <Separator />

                <div className="space-y-3">
                  <div className="space-y-0.5">
                    <Label className="text-base">Custom Vocabulary</Label>
                    <p className="text-sm text-muted-foreground">
                      Add specific words or phrases to improve accuracy (comma separated). Used as Keyterms Prompting for Assembly AI Universal-3-Pro.
                    </p>
                  </div>
                  <Textarea
                    value={customVocabulary}
                    onChange={(e) => setCustomVocabulary(e.target.value)}
                    onBlur={handleCustomVocabBlur}
                    placeholder="e.g. Replit, TypeScript, OpenAI, specific product names..."
                    className="min-h-[80px] font-mono text-sm resize-none bg-secondary/20"
                  />
                </div>

                <Separator />

                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label className="text-base">Visualizer Bars</Label>
                      <p className="text-sm text-muted-foreground">Number of bars in the audio visualizer ({visualizerBarCount})</p>
                    </div>
                    <div className="flex items-center gap-3 w-[200px]">
                      <BarChart3 className="w-4 h-4 text-muted-foreground" />
                      <Slider
                        value={[visualizerBarCount]}
                        onValueChange={handleVisualizerBarCountChange}
                        min={16}
                        max={128}
                        step={1}
                        className="flex-1"
                      />
                    </div>
                  </div>
                </div>

                <Separator />

                <div className="space-y-3">
                  <div className="space-y-0.5">
                    <Label className="text-base">Summarization Prompt</Label>
                    <p className="text-sm text-muted-foreground">Custom prompt used by the LLM to summarize transcripts</p>
                  </div>
                  <Textarea
                    value={summarizationPrompt}
                    onChange={(e) => setSummarizationPrompt(e.target.value)}
                    onBlur={handleSummarizationPromptBlur}
                    placeholder="e.g. Summarize the following transcript in bullet points, focusing on key decisions and action items..."
                    className="min-h-[100px] text-sm resize-none bg-secondary/20"
                  />
                </div>

              </div>
            </AccordionContent>
          </div>
        </AccordionItem>

        {/* API Keys */}
        <AccordionItem value="api-keys" className="border-0">
          <div className="neu-panel-raised bg-card rounded-xl overflow-hidden">
            <AccordionTrigger className="px-6 py-4 hover:no-underline">
              <div className="flex items-center gap-3">
                <div className="p-2 rounded-lg bg-amber-500/10 text-amber-600 dark:text-amber-400">
                  <Key className="w-5 h-5" />
                </div>
                <div className="text-left">
                  <h2 className="font-semibold text-foreground">API Configuration</h2>
                  <p className="text-sm text-muted-foreground">Manage API keys for STT and AI services</p>
                </div>
              </div>
            </AccordionTrigger>
            <AccordionContent>
              <div className="px-6 pb-6 space-y-4">
                <div className="space-y-2">
                  <Label>OpenAI API Key</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        type={showOpenAIKey ? "text" : "password"}
                        value={openAIKey}
                        onChange={(e) => setOpenAIKey(e.target.value)}
                        className="font-mono text-sm pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowOpenAIKey(!showOpenAIKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showOpenAIKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button
                      variant={savedKeys['OpenAI'] ? "default" : "outline"}
                      onClick={() => handleSaveApiKey('OpenAI')}
                      className={savedKeys['OpenAI'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                    >
                      {savedKeys['OpenAI'] ? <Check className="w-4 h-4 mr-2" /> : null}
                      {savedKeys['OpenAI'] ? "Saved" : "Save"}
                    </Button>
                  </div>
                  <p className="text-xs text-muted-foreground">Used for summarization and advanced analysis</p>
                </div>

                <div className="space-y-2 pt-2">
                  <Label>Deepgram API Key</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        type={showDeepgramKey ? "text" : "password"}
                        value={deepgramKey}
                        onChange={(e) => setDeepgramKey(e.target.value)}
                        placeholder="Enter your Deepgram key"
                        className="font-mono text-sm pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowDeepgramKey(!showDeepgramKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showDeepgramKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button
                      variant={savedKeys['Deepgram'] ? "default" : "outline"}
                      onClick={() => handleSaveApiKey('Deepgram')}
                      className={savedKeys['Deepgram'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                    >
                      {savedKeys['Deepgram'] ? <Check className="w-4 h-4 mr-2" /> : null}
                      {savedKeys['Deepgram'] ? "Saved" : "Save"}
                    </Button>
                  </div>
                </div>

                <div className="space-y-2 pt-2">
                  <Label>AssemblyAI API Key (Universal-3-Pro)</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        type={showAssemblyAIKey ? "text" : "password"}
                        value={assemblyAIKey}
                        onChange={(e) => setAssemblyAIKey(e.target.value)}
                        placeholder="Enter your AssemblyAI key"
                        className="font-mono text-sm pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowAssemblyAIKey(!showAssemblyAIKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showAssemblyAIKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button
                      variant={savedKeys['AssemblyAI'] ? "default" : "outline"}
                      onClick={() => handleSaveApiKey('AssemblyAI')}
                      className={savedKeys['AssemblyAI'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                    >
                      {savedKeys['AssemblyAI'] ? <Check className="w-4 h-4 mr-2" /> : null}
                      {savedKeys['AssemblyAI'] ? "Saved" : "Save"}
                    </Button>
                  </div>
                </div>

                <div className="space-y-2 pt-2">
                  <Label>Gemini API Key</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        type={showGeminiKey ? "text" : "password"}
                        value={geminiKey}
                        onChange={(e) => setGeminiKey(e.target.value)}
                        placeholder="Enter your Gemini key"
                        className="font-mono text-sm pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowGeminiKey(!showGeminiKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showGeminiKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button
                      variant={savedKeys['Gemini'] ? "default" : "outline"}
                      onClick={() => handleSaveApiKey('Gemini')}
                      className={savedKeys['Gemini'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                    >
                      {savedKeys['Gemini'] ? <Check className="w-4 h-4 mr-2" /> : null}
                      {savedKeys['Gemini'] ? "Saved" : "Save"}
                    </Button>
                  </div>
                </div>

                <div className="space-y-2 pt-2">
                  <Label>YouTube API Key</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        type={showYoutubeKey ? "text" : "password"}
                        value={youtubeKey}
                        onChange={(e) => setYoutubeKey(e.target.value)}
                        placeholder="Enter your YouTube key"
                        className="font-mono text-sm pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowYoutubeKey(!showYoutubeKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showYoutubeKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button
                      variant={savedKeys["YouTube"] ? "default" : "outline"}
                      onClick={() => handleSaveApiKey("YouTube")}
                      className={savedKeys["YouTube"] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                    >
                      {savedKeys["YouTube"] ? <Check className="w-4 h-4 mr-2" /> : null}
                      {savedKeys["YouTube"] ? "Saved" : "Save"}
                    </Button>
                  </div>
                  <p className="text-xs text-muted-foreground">Used for the Youtube tab (search / metadata).</p>
                </div>

                <div className="space-y-2 pt-2">
                  <Label>Soniox API Key</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        type={showSonioxKey ? "text" : "password"}
                        value={sonioxKey}
                        onChange={(e) => setSonioxKey(e.target.value)}
                        placeholder="Enter your Soniox key"
                        className="font-mono text-sm pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowSonioxKey(!showSonioxKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showSonioxKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button
                      variant={savedKeys['Soniox'] ? "default" : "outline"}
                      onClick={() => handleSaveApiKey('Soniox')}
                      className={savedKeys['Soniox'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                    >
                      {savedKeys['Soniox'] ? <Check className="w-4 h-4 mr-2" /> : null}
                      {savedKeys['Soniox'] ? "Saved" : "Save"}
                    </Button>
                  </div>
                </div>

                <div className="space-y-2 pt-2">
                  <Label>Mistral API Key</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        type={showMistralKey ? "text" : "password"}
                        value={mistralKey}
                        onChange={(e) => setMistralKey(e.target.value)}
                        placeholder="Enter your Mistral key"
                        className="font-mono text-sm pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowMistralKey(!showMistralKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showMistralKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button
                      variant={savedKeys['Mistral'] ? "default" : "outline"}
                      onClick={() => handleSaveApiKey('Mistral')}
                      className={savedKeys['Mistral'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                    >
                      {savedKeys['Mistral'] ? <Check className="w-4 h-4 mr-2" /> : null}
                      {savedKeys['Mistral'] ? "Saved" : "Save"}
                    </Button>
                  </div>
                </div>

                <div className="space-y-2 pt-2">
                  <Label>ElevenLabs API Key (via Fal.ai)</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        type={showElevenLabsKey ? "text" : "password"}
                        value={elevenLabsKey}
                        onChange={(e) => setElevenLabsKey(e.target.value)}
                        placeholder="Enter your Fal.ai key"
                        className="font-mono text-sm pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowElevenLabsKey(!showElevenLabsKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showElevenLabsKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button
                      variant={savedKeys['ElevenLabs'] ? "default" : "outline"}
                      onClick={() => handleSaveApiKey('ElevenLabs')}
                      className={savedKeys['ElevenLabs'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                    >
                      {savedKeys['ElevenLabs'] ? <Check className="w-4 h-4 mr-2" /> : null}
                      {savedKeys['ElevenLabs'] ? "Saved" : "Save"}
                    </Button>
                  </div>
                </div>

                <div className="space-y-2 pt-2">
                  <Label>Azure Speech Key</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        type={showAzureKey ? "text" : "password"}
                        value={azureKey}
                        onChange={(e) => setAzureKey(e.target.value)}
                        placeholder="Enter your Azure key"
                        className="font-mono text-sm pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowAzureKey(!showAzureKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showAzureKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button
                      variant={savedKeys['Azure'] ? "default" : "outline"}
                      onClick={() => handleSaveApiKey('Azure')}
                      className={savedKeys['Azure'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                    >
                      {savedKeys['Azure'] ? <Check className="w-4 h-4 mr-2" /> : null}
                      {savedKeys['Azure'] ? "Saved" : "Save"}
                    </Button>
                  </div>
                </div>

                <div className="space-y-2 pt-2">
                  <Label>Azure Speech Region</Label>
                  <Input
                    value={azureRegion}
                    onChange={(e) => setAzureRegion(e.target.value)}
                    placeholder="e.g. westus"
                    className="font-mono text-sm"
                  />
                  <p className="text-xs text-muted-foreground">Required for Azure Speech STT.</p>
                </div>

                <div className="space-y-2 pt-2">
                  <Label>Gladia API Key</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        type={showGladiaKey ? "text" : "password"}
                        value={gladiaKey}
                        onChange={(e) => setGladiaKey(e.target.value)}
                        placeholder="Enter your Gladia key"
                        className="font-mono text-sm pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowGladiaKey(!showGladiaKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showGladiaKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button
                      variant={savedKeys['Gladia'] ? "default" : "outline"}
                      onClick={() => handleSaveApiKey('Gladia')}
                      className={savedKeys['Gladia'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                    >
                      {savedKeys['Gladia'] ? <Check className="w-4 h-4 mr-2" /> : null}
                      {savedKeys['Gladia'] ? "Saved" : "Save"}
                    </Button>
                  </div>
                </div>

                <div className="space-y-2 pt-2">
                  <Label>Groq API Key</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        type={showGroqKey ? "text" : "password"}
                        value={groqKey}
                        onChange={(e) => setGroqKey(e.target.value)}
                        placeholder="Enter your Groq key"
                        className="font-mono text-sm pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowGroqKey(!showGroqKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showGroqKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button
                      variant={savedKeys['Groq'] ? "default" : "outline"}
                      onClick={() => handleSaveApiKey('Groq')}
                      className={savedKeys['Groq'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                    >
                      {savedKeys['Groq'] ? <Check className="w-4 h-4 mr-2" /> : null}
                      {savedKeys['Groq'] ? "Saved" : "Save"}
                    </Button>
                  </div>
                </div>

                <div className="space-y-2 pt-2">
                  <Label>AWS Access Key ID (Transcribe)</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        type={showAwsKey ? "text" : "password"}
                        value={awsKey}
                        onChange={(e) => setAwsKey(e.target.value)}
                        placeholder="Enter your AWS Access Key"
                        className="font-mono text-sm pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowAwsKey(!showAwsKey)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showAwsKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button
                      variant={savedKeys['AWS'] ? "default" : "outline"}
                      onClick={() => handleSaveApiKey('AWS')}
                      className={savedKeys['AWS'] ? "bg-green-600 hover:bg-green-700 text-white border-green-600" : ""}
                    >
                      {savedKeys['AWS'] ? <Check className="w-4 h-4 mr-2" /> : null}
                      {savedKeys['AWS'] ? "Saved" : "Save"}
                    </Button>
                  </div>
                </div>
              </div>
            </AccordionContent>
          </div>
        </AccordionItem>

      </Accordion>
    </div>
  );
}
