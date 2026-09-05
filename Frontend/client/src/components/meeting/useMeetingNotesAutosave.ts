import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import type { MeetingNote } from "@/lib/api-types";
import { backendBaseUrl, backendSessionToken } from "@/lib/backend";
import { friendlyError, responseErrorMessage } from "@/lib/request-errors";

export const MEETING_NOTE_MAX_UTF8_BYTES = 48 * 1024;
export const MEETING_NOTE_KEEPALIVE_PAYLOAD_MAX_BYTES = 56 * 1024;
export const MEETING_NOTE_WRITER_STORAGE_KEY = "scriber-meeting-note-writer-v1";
export const MEETING_NOTE_GENERATION_STORAGE_KEY = "scriber-meeting-note-generation-v1";

const MAX_WRITE_GENERATION = Number.MAX_SAFE_INTEGER;
const WRITER_ID_PATTERN = /^[A-Za-z0-9_-]{16,96}$/;
const textEncoder = new TextEncoder();

let inMemoryWriterId = "";
let inMemoryGeneration = 0;

export type MeetingNotesAutosaveStatus = "saved" | "dirty" | "saving" | "error";

export interface MeetingNoteWriteOrder {
  generation: number;
  writerId: string;
}

export type SaveMeetingNote = (
  meetingId: string,
  body: string,
  writeOrder: MeetingNoteWriteOrder,
) => Promise<MeetingNote>;

interface MeetingNotesAutosaveSnapshot {
  body: string;
  error: Error | null;
  isDirty: boolean;
  status: MeetingNotesAutosaveStatus;
  teardownSafe: boolean;
}

interface MeetingNotesQueueCallbacks {
  nextWriteGeneration: (floor: number) => number;
  onError: (error: Error, meetingId: string) => void;
  onSaved: (note: MeetingNote, meetingId: string) => void;
  recordGenerationFloor: (generation: number) => void;
  saveNote: SaveMeetingNote;
  saveNoteOnTeardown: SaveMeetingNote;
  writerId: string;
}

export interface UseMeetingNotesAutosaveOptions {
  debounceMs?: number;
  initialBody: string;
  meetingId: string;
  nextWriteGeneration?: (floor: number) => number;
  onError: (error: Error, meetingId: string) => void;
  onSaved: (note: MeetingNote, meetingId: string) => void;
  saveNote: SaveMeetingNote;
  saveNoteOnTeardown?: SaveMeetingNote;
  writerId?: string;
}

export interface UseMeetingNotesAutosaveResult extends MeetingNotesAutosaveSnapshot {
  retry: () => void;
  setBody: (body: string) => void;
}

function utf8ByteLength(value: string): number {
  return textEncoder.encode(value).byteLength;
}

function normalizeBody(body: string): string {
  return body.trim();
}

function storageValue(key: string): string {
  try {
    return window.localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

function storeValue(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // A stable module-level fallback still preserves ordering for this WebView.
  }
}

function createWriterId(): string {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  if (randomUuid) return randomUuid;
  const randomPart = Math.random().toString(36).slice(2);
  return `writer_${Date.now().toString(36)}_${randomPart.padEnd(16, "0")}`;
}

export function persistentMeetingNoteWriterId(): string {
  const stored = storageValue(MEETING_NOTE_WRITER_STORAGE_KEY);
  if (WRITER_ID_PATTERN.test(stored)) {
    inMemoryWriterId = stored;
    return stored;
  }
  if (!WRITER_ID_PATTERN.test(inMemoryWriterId)) {
    inMemoryWriterId = createWriterId();
  }
  storeValue(MEETING_NOTE_WRITER_STORAGE_KEY, inMemoryWriterId);
  return inMemoryWriterId;
}

function parsedStoredGeneration(): number {
  const parsed = Number(storageValue(MEETING_NOTE_GENERATION_STORAGE_KEY));
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : 0;
}

export function recordMeetingNoteGenerationFloor(generation: number): void {
  if (!Number.isSafeInteger(generation) || generation <= 0) return;
  inMemoryGeneration = Math.max(inMemoryGeneration, generation);
  storeValue(MEETING_NOTE_GENERATION_STORAGE_KEY, String(inMemoryGeneration));
}

export function nextPersistentMeetingNoteGeneration(floor = 0): number {
  const timestampFloor = Math.floor(Date.now() * 1_000);
  const generation = Math.max(timestampFloor, floor + 1, inMemoryGeneration + 1, parsedStoredGeneration() + 1);
  if (!Number.isSafeInteger(generation) || generation > MAX_WRITE_GENERATION) {
    throw new Error("Meeting note write generation is exhausted.");
  }
  recordMeetingNoteGenerationFloor(generation);
  return generation;
}

function workspaceNotePayload(body: string, writeOrder: MeetingNoteWriteOrder): string {
  const normalizedBody = normalizeBody(body);
  if (utf8ByteLength(normalizedBody) > MEETING_NOTE_MAX_UTF8_BYTES) {
    throw new Error("Meeting note exceeds the 48 KiB UTF-8 size limit.");
  }
  if (
    !WRITER_ID_PATTERN.test(writeOrder.writerId) ||
    !Number.isSafeInteger(writeOrder.generation) ||
    writeOrder.generation <= 0
  ) {
    throw new Error("Meeting note write order is invalid.");
  }
  return JSON.stringify({
    id: "workspace",
    body: normalizedBody,
    writerId: writeOrder.writerId,
    writeGeneration: writeOrder.generation,
  });
}

export function canKeepaliveWorkspaceMeetingNote(body: string, writerId: string): boolean {
  try {
    const payload = workspaceNotePayload(body, {
      writerId,
      generation: MAX_WRITE_GENERATION,
    });
    return utf8ByteLength(payload) <= MEETING_NOTE_KEEPALIVE_PAYLOAD_MAX_BYTES;
  } catch {
    return false;
  }
}

async function sendWorkspaceMeetingNote(
  meetingId: string,
  body: string,
  writeOrder: MeetingNoteWriteOrder,
  preferKeepalive: boolean,
): Promise<MeetingNote> {
  const normalizedMeetingId = meetingId.trim();
  if (!normalizedMeetingId) {
    throw new Error("Meeting id is required.");
  }

  const endpoint = new URL(
    `/api/meetings/${encodeURIComponent(normalizedMeetingId)}/notes`,
    `${backendBaseUrl.replace(/\/+$/, "")}/`,
  );
  endpoint.username = "";
  endpoint.password = "";
  endpoint.search = "";
  endpoint.hash = "";

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (backendSessionToken) {
    headers["X-Scriber-Token"] = backendSessionToken;
  }
  const payload = workspaceNotePayload(body, writeOrder);
  const keepalive = preferKeepalive && utf8ByteLength(payload) <= MEETING_NOTE_KEEPALIVE_PAYLOAD_MAX_BYTES;

  try {
    const response = await fetch(endpoint, {
      method: "PUT",
      headers,
      body: payload,
      cache: "no-store",
      credentials: "include",
      keepalive,
      referrerPolicy: "no-referrer",
    });
    if (!response.ok) {
      throw new Error(await responseErrorMessage(response));
    }
    return response.json() as Promise<MeetingNote>;
  } catch (error) {
    throw new Error(friendlyError(error, "The note could not be saved."), { cause: error });
  }
}

export function saveWorkspaceMeetingNote(
  meetingId: string,
  body: string,
  writeOrder: MeetingNoteWriteOrder,
): Promise<MeetingNote> {
  return sendWorkspaceMeetingNote(meetingId, body, writeOrder, false);
}

export function saveWorkspaceMeetingNoteOnTeardown(
  meetingId: string,
  body: string,
  writeOrder: MeetingNoteWriteOrder,
): Promise<MeetingNote> {
  return sendWorkspaceMeetingNote(meetingId, body, writeOrder, true);
}

type QueueListener = () => void;

const meetingSaveLaneTails = new Map<string, Promise<void>>();

/**
 * Serialize ordinary note writes across component lifetimes. Page teardown
 * writes start synchronously and may bypass this lane; the durable generation
 * compare-and-set in MeetingStore provides the authoritative cross-lane order.
 */
function runInMeetingSaveLane<T>(meetingId: string, task: () => Promise<T>): Promise<T> {
  const previous = meetingSaveLaneTails.get(meetingId) ?? Promise.resolve();
  const operation = previous.catch(() => undefined).then(task);
  const tail = operation.then(
    () => undefined,
    () => undefined,
  );
  meetingSaveLaneTails.set(meetingId, tail);
  void tail.then(() => {
    if (meetingSaveLaneTails.get(meetingId) === tail) {
      meetingSaveLaneTails.delete(meetingId);
    }
  });
  return operation;
}

interface PendingWrite {
  body: string;
  generation: number;
}

class MeetingNotesSaveQueue {
  readonly meetingId: string;

  private callbacks: MeetingNotesQueueCallbacks;
  private draftBody: string;
  private error: Error | null = null;
  private inFlightWrite: PendingWrite | null = null;
  private lastAppliedWrite = 0;
  private lastErrorWrite = 0;
  private listeners = new Set<QueueListener>();
  private onPrunable: (queue: MeetingNotesSaveQueue) => void;
  private queuedBody: string | null = null;
  private savedBody: string;
  private snapshot: MeetingNotesAutosaveSnapshot;
  private teardownWrite: PendingWrite | null = null;

  constructor(
    meetingId: string,
    initialBody: string,
    callbacks: MeetingNotesQueueCallbacks,
    onPrunable: (queue: MeetingNotesSaveQueue) => void,
  ) {
    this.meetingId = meetingId;
    this.callbacks = callbacks;
    this.draftBody = initialBody;
    this.onPrunable = onPrunable;
    this.savedBody = normalizeBody(initialBody);
    this.snapshot = this.buildSnapshot();
  }

  getSnapshot = (): MeetingNotesAutosaveSnapshot => this.snapshot;

  subscribe = (listener: QueueListener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  setCallbacks(callbacks: MeetingNotesQueueCallbacks): void {
    this.callbacks = callbacks;
  }

  hydrate(initialBody: string): void {
    const normalizedInitialBody = normalizeBody(initialBody);
    if (
      this.error ||
      this.inFlightWrite !== null ||
      this.queuedBody !== null ||
      normalizeBody(this.draftBody) !== this.savedBody ||
      normalizedInitialBody === this.savedBody
    ) {
      return;
    }
    this.draftBody = initialBody;
    this.savedBody = normalizedInitialBody;
    this.emit();
  }

  setDraft(body: string): void {
    this.draftBody = body;
    this.error = null;
    this.emit();
  }

  isPrunable(): boolean {
    return (
      this.error === null &&
      this.inFlightWrite === null &&
      this.queuedBody === null &&
      this.teardownWrite === null &&
      normalizeBody(this.draftBody) === this.savedBody
    );
  }

  requestSave = (): void => {
    const targetBody = normalizeBody(this.draftBody);
    this.error = null;

    if (this.inFlightWrite === null && this.teardownWrite === null && targetBody === this.savedBody) {
      this.queuedBody = null;
      this.emit();
      return;
    }

    if (targetBody === this.inFlightWrite?.body || targetBody === this.teardownWrite?.body) {
      this.queuedBody = null;
    } else {
      this.queuedBody = targetBody;
    }
    this.emit();
    void this.drain();
  };

  flush = (): void => {
    if (this.error || this.queuedBody !== null || normalizeBody(this.draftBody) !== this.savedBody) {
      this.requestSave();
    }
  };

  flushForTeardown = (): void => {
    const body = normalizeBody(this.draftBody);
    // Compare against the newest effective write. Matching the last saved body
    // or a superseded request does not protect a revert from a later write.
    let latestBody = this.savedBody;
    let latestGeneration = this.lastAppliedWrite;
    for (const pending of [this.inFlightWrite, this.teardownWrite]) {
      if (pending && pending.generation > latestGeneration) {
        latestBody = pending.body;
        latestGeneration = pending.generation;
      }
    }
    this.queuedBody = null;
    if (body === latestBody) {
      this.emit();
      return;
    }

    this.error = null;
    const generation = this.nextGeneration();
    const pending = { body, generation };
    this.teardownWrite = pending;
    this.emit();

    let operation: Promise<MeetingNote>;
    try {
      // Start fetch directly in the pagehide callback. The durable generation
      // CAS makes this safe even while an older ordinary request is in flight.
      operation = this.callbacks.saveNoteOnTeardown(this.meetingId, body, {
        writerId: this.callbacks.writerId,
        generation,
      });
    } catch (error) {
      operation = Promise.reject(error);
    }

    void operation
      .then(
        (note) => {
          this.applySuccess(note, pending);
        },
        (error) => {
          this.applyError(error, pending);
        },
      )
      .finally(() => {
        if (this.teardownWrite?.generation === generation) {
          this.teardownWrite = null;
        }
        this.queueLatestDraftIfNeeded();
        this.emit();
        void this.drain();
      })
      .catch(() => undefined);
  };

  private nextGeneration(): number {
    const floor = Math.max(this.lastAppliedWrite, this.lastErrorWrite);
    const generation = this.callbacks.nextWriteGeneration(floor);
    this.callbacks.recordGenerationFloor(generation);
    return generation;
  }

  private responseGeneration(note: MeetingNote, requestedGeneration: number): number {
    const generation = note.writeGeneration;
    if (typeof generation === "number" && Number.isSafeInteger(generation) && generation > 0) {
      return generation;
    }
    return requestedGeneration;
  }

  private applySuccess(note: MeetingNote, pending: PendingWrite): void {
    const generation = this.responseGeneration(note, pending.generation);
    this.callbacks.recordGenerationFloor(generation);
    if (generation < this.lastAppliedWrite) return;

    const authoritativeBody = normalizeBody(note.body);
    const duplicate = generation === this.lastAppliedWrite && authoritativeBody === this.savedBody;
    this.lastAppliedWrite = generation;
    this.savedBody = authoritativeBody;
    if (generation >= this.lastErrorWrite) {
      this.error = null;
    }
    if (!duplicate) {
      this.callbacks.onSaved(note, this.meetingId);
    }
    this.queueLatestDraftIfNeeded();
  }

  private applyError(error: unknown, pending: PendingWrite): void {
    if (pending.generation < this.lastAppliedWrite || pending.generation < this.lastErrorWrite) {
      return;
    }
    const saveError = error instanceof Error ? error : new Error(String(error));
    this.lastErrorWrite = pending.generation;
    this.error = saveError;
    this.callbacks.onError(saveError, this.meetingId);
  }

  private queueLatestDraftIfNeeded(): void {
    const latestBody = normalizeBody(this.draftBody);
    if (
      latestBody !== this.savedBody &&
      latestBody !== this.inFlightWrite?.body &&
      latestBody !== this.teardownWrite?.body
    ) {
      this.queuedBody = latestBody;
    }
  }

  private buildSnapshot(): MeetingNotesAutosaveSnapshot {
    const isDirty = normalizeBody(this.draftBody) !== this.savedBody || this.queuedBody !== null;
    let status: MeetingNotesAutosaveStatus = "saved";
    if (this.error) {
      status = "error";
    } else if (this.inFlightWrite !== null || this.teardownWrite !== null) {
      status = "saving";
    } else if (isDirty) {
      status = "dirty";
    }
    return {
      body: this.draftBody,
      error: this.error,
      isDirty,
      status,
      teardownSafe: canKeepaliveWorkspaceMeetingNote(this.draftBody, this.callbacks.writerId),
    };
  }

  private emit(): void {
    this.snapshot = this.buildSnapshot();
    this.listeners.forEach((listener) => listener());
    if (this.isPrunable()) {
      this.onPrunable(this);
    }
  }

  private async drain(): Promise<void> {
    if (this.inFlightWrite !== null || this.teardownWrite !== null || this.error) {
      return;
    }

    while (this.queuedBody !== null) {
      const body = this.queuedBody;
      this.queuedBody = null;

      if (body === this.savedBody) {
        this.emit();
        continue;
      }

      const pending = {
        body,
        generation: this.nextGeneration(),
      };
      this.inFlightWrite = pending;
      this.emit();
      try {
        const note = await runInMeetingSaveLane(this.meetingId, () =>
          this.callbacks.saveNote(this.meetingId, body, {
            writerId: this.callbacks.writerId,
            generation: pending.generation,
          }),
        );
        this.applySuccess(note, pending);
      } catch (error) {
        this.applyError(error, pending);
      } finally {
        if (this.inFlightWrite?.generation === pending.generation) {
          this.inFlightWrite = null;
        }
        this.queueLatestDraftIfNeeded();
        this.emit();
      }

      if (this.error) return;
    }
  }
}

export function useMeetingNotesAutosave({
  debounceMs = 700,
  initialBody,
  meetingId,
  nextWriteGeneration,
  onError,
  onSaved,
  saveNote,
  saveNoteOnTeardown = saveWorkspaceMeetingNoteOnTeardown,
  writerId,
}: UseMeetingNotesAutosaveOptions): UseMeetingNotesAutosaveResult {
  const stableWriterId = useRef(writerId || persistentMeetingNoteWriterId());
  const callbackRef = useRef<MeetingNotesQueueCallbacks>({
    nextWriteGeneration: (floor) => nextWriteGeneration?.(floor) ?? nextPersistentMeetingNoteGeneration(floor),
    onError,
    onSaved,
    recordGenerationFloor: nextWriteGeneration ? () => undefined : recordMeetingNoteGenerationFloor,
    saveNote,
    saveNoteOnTeardown,
    writerId: stableWriterId.current,
  });
  callbackRef.current = {
    nextWriteGeneration: (floor) => nextWriteGeneration?.(floor) ?? nextPersistentMeetingNoteGeneration(floor),
    onError,
    onSaved,
    recordGenerationFloor: nextWriteGeneration ? () => undefined : recordMeetingNoteGenerationFloor,
    saveNote,
    saveNoteOnTeardown,
    writerId: stableWriterId.current,
  };

  const queues = useRef(new Map<string, MeetingNotesSaveQueue>());
  const activeQueue = useRef<MeetingNotesSaveQueue | null>(null);
  const queue = useMemo(() => {
    const existing = queues.current.get(meetingId);
    if (existing) return existing;
    const created = new MeetingNotesSaveQueue(
      meetingId,
      initialBody,
      {
        nextWriteGeneration: (floor) => callbackRef.current.nextWriteGeneration(floor),
        onError: (error, id) => callbackRef.current.onError(error, id),
        onSaved: (note, id) => callbackRef.current.onSaved(note, id),
        recordGenerationFloor: (generation) => callbackRef.current.recordGenerationFloor(generation),
        saveNote: (id, body, order) => callbackRef.current.saveNote(id, body, order),
        saveNoteOnTeardown: (id, body, order) => callbackRef.current.saveNoteOnTeardown(id, body, order),
        writerId: stableWriterId.current,
      },
      (candidate) => {
        if (
          activeQueue.current !== candidate &&
          candidate.isPrunable() &&
          queues.current.get(candidate.meetingId) === candidate
        ) {
          queues.current.delete(candidate.meetingId);
        }
      },
    );
    queues.current.set(meetingId, created);
    return created;
  }, [initialBody, meetingId]);

  queue.setCallbacks({
    nextWriteGeneration: (floor) => callbackRef.current.nextWriteGeneration(floor),
    onError: (error, id) => callbackRef.current.onError(error, id),
    onSaved: (note, id) => callbackRef.current.onSaved(note, id),
    recordGenerationFloor: (generation) => callbackRef.current.recordGenerationFloor(generation),
    saveNote: (id, body, order) => callbackRef.current.saveNote(id, body, order),
    saveNoteOnTeardown: (id, body, order) => callbackRef.current.saveNoteOnTeardown(id, body, order),
    writerId: stableWriterId.current,
  });

  const snapshot = useSyncExternalStore(queue.subscribe, queue.getSnapshot, queue.getSnapshot);

  useEffect(() => {
    queue.hydrate(initialBody);
  }, [initialBody, queue]);

  useEffect(() => {
    activeQueue.current = queue;
    queues.current.forEach((candidate, id) => {
      if (candidate !== queue && candidate.isPrunable()) {
        queues.current.delete(id);
      }
    });

    const handlePageHide = () => {
      queue.flushForTeardown();
    };
    window.addEventListener("pagehide", handlePageHide);
    return () => {
      window.removeEventListener("pagehide", handlePageHide);
      queue.flush();
    };
  }, [queue]);

  useEffect(() => {
    if (!snapshot.isDirty) return;
    const handle = window.setTimeout(queue.requestSave, debounceMs);
    return () => window.clearTimeout(handle);
  }, [debounceMs, queue, snapshot.body, snapshot.isDirty]);

  const setBody = useCallback(
    (body: string) => {
      queue.setDraft(body);
    },
    [queue],
  );
  const retry = useCallback(() => {
    queue.requestSave();
  }, [queue]);

  return {
    ...snapshot,
    retry,
    setBody,
  };
}
