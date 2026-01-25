import { User, CreditCard, Keyboard, Shield, Zap, Globe, ChevronRight, LogOut, Eye, EyeOff, Check, Mic, Mic2, MousePointerClick, ToggleLeft, AudioLines, BarChart3, Power, Key, Settings2, Star, Download, Trash2, Loader2 } from "lucide-react";
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

export default function Settings() {
  const [openAIKey, setOpenAIKey] = useState("");
  const [deepgramKey, setDeepgramKey] = useState("");
  const [assemblyAIKey, setAssemblyAIKey] = useState("");
  const [geminiKey, setGeminiKey] = useState("");
  const [youtubeKey, setYoutubeKey] = useState("");
  const [sonioxKey, setSonioxKey] = useState("");
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
  const [micAlwaysOn, setMicAlwaysOn] = useState(false);
  const [favoriteMic, setFavoriteMic] = useState("");

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
      return service || "soniox-realtime";
    };

    const load = async () => {
      try {
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
        await loadOnnxModels();
        await loadNemoModels();
      } catch (e: any) {
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
  }, [toast, loadOnnxModels, loadNemoModels]);

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
  }, [loadOnnxModels, loadNemoModels]);

  useSharedWebSocket(handleWsMessage);

  const selectedOnnxModel = onnxModels.find((m) => m.id === onnxModel) || onnxModels[0];
  const selectedNemoModel = nemoModels.find((m) => m.id === nemoModel) || nemoModels[0];
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
    <div className="max-w-screen-md mx-auto px-4 py-6 md:py-8">
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
                  <div className="space-y-2">
                    {inputDevices.length === 0 ? (
                      <div className="text-sm text-muted-foreground py-2">Loading devices...</div>
                    ) : (
                      inputDevices.map((device, index) => {
                        const deviceValue = device.deviceId || `device-${index}`;
                        const isSelected = selectedDeviceId === deviceValue;
                        const isFavorite = favoriteMic === deviceValue;
                        return (
                          <div
                            key={`${deviceValue}-${index}`}
                            className={cn(
                              "flex items-center gap-3 p-3 rounded-lg border cursor-pointer transition-all",
                              isSelected
                                ? "border-primary bg-primary/5"
                                : "border-border/60 hover:border-primary/50 hover:bg-accent/5"
                            )}
                            onClick={() => handleMicDeviceChange(deviceValue)}
                          >
                            <AudioLines className={cn(
                              "w-4 h-4 shrink-0",
                              isSelected ? "text-primary" : "text-muted-foreground"
                            )} />
                            <span className={cn(
                              "flex-1 text-sm truncate",
                              isSelected ? "font-medium" : ""
                            )}>
                              {device.label || `Device ${index + 1}`}
                            </span>
                            {isSelected && <Check className="w-4 h-4 text-primary shrink-0" />}
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                handleSetFavoriteMic(deviceValue);
                              }}
                              className={cn(
                                "p-1.5 rounded-md transition-all shrink-0",
                                isFavorite
                                  ? "text-amber-500 hover:bg-amber-500/10"
                                  : "text-muted-foreground/40 hover:text-muted-foreground hover:bg-accent"
                              )}
                              title={isFavorite ? "Remove from favorites" : "Set as favorite"}
                            >
                              <Star className={cn("w-4 h-4", isFavorite && "fill-amber-500")} />
                            </button>
                          </div>
                        );
                      })
                    )}
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
                  <Select value={transcriptionModel} onValueChange={handleTranscriptionModelChange}>
                    <SelectTrigger className="w-[320px]">
                      <SelectValue placeholder="Select model" />
                    </SelectTrigger>
                      <SelectContent>
                      <SelectItem value="onnx_local">Local (ONNX) - No API Key</SelectItem>
                      <SelectItem value="nemo_local">Local (NeMo) - Primeline</SelectItem>
                      <SelectItem value="soniox-realtime">Soniox Realtime</SelectItem>
                      <SelectItem value="soniox-async">Soniox Async</SelectItem>
                      <SelectItem value="assemblyai">AssemblyAI</SelectItem>
                      <SelectItem value="deepgram">Deepgram</SelectItem>
                      <SelectItem value="openai">OpenAI</SelectItem>
                      <SelectItem value="azure">Azure Speech</SelectItem>
                      <SelectItem value="gladia">Gladia</SelectItem>
                      <SelectItem value="groq">Groq</SelectItem>
                      <SelectItem value="speechmatics">Speechmatics</SelectItem>
                      <SelectItem value="elevenlabs">ElevenLabs</SelectItem>
                      <SelectItem value="google">Google Cloud STT</SelectItem>
                      <SelectItem value="aws">AWS Transcribe</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                <Separator />

                <div className="space-y-3">
                  <div className="space-y-0.5">
                    <Label className="text-base">Local Models</Label>
                    <p className="text-sm text-muted-foreground">Run speech recognition locally without API keys</p>
                  </div>

                  {onnxAvailable === null && (
                    <div className="text-sm text-muted-foreground">Loading local models...</div>
                  )}

                  {onnxAvailable === false && (
                    <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                      {onnxMessage || "onnx-asr not installed. Run: pip install onnx-asr[cpu,hub]"}
                    </div>
                  )}

                  {onnxAvailable && (
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

                  {nemoAvailable === null && (
                    <div className="text-sm text-muted-foreground">Loading NeMo models...</div>
                  )}

                  {nemoAvailable === false && (
                    <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                      {nemoMessage || "NeMo toolkit not installed. Run: pip install nemo_toolkit[asr]"}
                    </div>
                  )}

                  {nemoAvailable && (
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
                  <Select value={language} onValueChange={handleLanguageChange}>
                    <SelectTrigger className="w-[320px]">
                      <SelectValue placeholder="Select language" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="auto">
                        <div className="flex items-center gap-2">
                          <Globe className="w-4 h-4 text-muted-foreground" />
                          <span>Auto-detect</span>
                        </div>
                      </SelectItem>
                      <SelectItem value="en">
                        <div className="flex items-center gap-2">
                          <span className="fi fi-us rounded-sm"></span>
                          <span>English</span>
                        </div>
                      </SelectItem>
                      <SelectItem value="es">
                        <div className="flex items-center gap-2">
                          <span className="fi fi-es rounded-sm"></span>
                          <span>Spanish</span>
                        </div>
                      </SelectItem>
                      <SelectItem value="fr">
                        <div className="flex items-center gap-2">
                          <span className="fi fi-fr rounded-sm"></span>
                          <span>French</span>
                        </div>
                      </SelectItem>
                      <SelectItem value="de">
                        <div className="flex items-center gap-2">
                          <span className="fi fi-de rounded-sm"></span>
                          <span>German</span>
                        </div>
                      </SelectItem>
                      <SelectItem value="it">
                        <div className="flex items-center gap-2">
                          <span className="fi fi-it rounded-sm"></span>
                          <span>Italian</span>
                        </div>
                      </SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                <Separator />

                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label className="text-base">Summarization Model</Label>
                    <p className="text-sm text-muted-foreground">Select model for summarizing transcripts</p>
                  </div>
                  <Select value={summarizationModel} onValueChange={handleSummarizationModelChange}>
                    <SelectTrigger className="w-[320px]">
                      <SelectValue placeholder="Select model" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="gemini-3-flash-preview">Gemini 3.0 Flash Preview (Recommended)</SelectItem>
                      <SelectItem value="gemini-3-pro-preview">Gemini 3 Pro</SelectItem>
                      <SelectItem value="gpt-5.2">OpenAI GPT 5.2</SelectItem>
                      <SelectItem value="gpt-5-mini">OpenAI GPT 5 Mini</SelectItem>
                      <SelectItem value="gpt-5-nano">OpenAI GPT 5 Nano</SelectItem>
                    </SelectContent>
                  </Select>
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
                    <p className="text-sm text-muted-foreground">Add specific words or phrases to improve accuracy (comma separated)</p>
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
                  <Label>AssemblyAI API Key</Label>
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
