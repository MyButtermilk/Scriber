import { useRef } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LocaleProvider, LANGUAGE_STORAGE_KEY } from "@/i18n";
import type { MeetingNote } from "@/lib/api-types";
import { setBackendBaseUrl, setBackendSessionToken } from "@/lib/backend";
import { MeetingNotesEditor } from "./MeetingNotesEditor";
import {
  MEETING_NOTE_GENERATION_STORAGE_KEY,
  MEETING_NOTE_KEEPALIVE_PAYLOAD_MAX_BYTES,
  MEETING_NOTE_WRITER_STORAGE_KEY,
  nextPersistentMeetingNoteGeneration,
  persistentMeetingNoteWriterId,
  recordMeetingNoteGenerationFloor,
  saveWorkspaceMeetingNote,
  useMeetingNotesAutosave,
  type MeetingNoteWriteOrder,
  type SaveMeetingNote,
} from "./useMeetingNotesAutosave";

interface Deferred<T> {
  promise: Promise<T>;
  reject: (reason?: unknown) => void;
  resolve: (value: T) => void;
}

const TEST_WRITER_ID = "writer_test_session_123";

function deferred<T>(): Deferred<T> {
  let reject!: (reason?: unknown) => void;
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function note(body: string, writeOrder?: Partial<MeetingNoteWriteOrder> & { applied?: boolean }): MeetingNote {
  return {
    id: "workspace",
    meetingId: "meeting-a",
    body,
    atMs: null,
    createdAt: "2026-07-27T12:00:00Z",
    updatedAt: "2026-07-27T12:00:00Z",
    ...(writeOrder?.generation === undefined ? {} : { writeGeneration: writeOrder.generation }),
    ...(writeOrder?.applied === undefined ? {} : { writeApplied: writeOrder.applied }),
  };
}

function generationSequence(start = 0): (floor: number) => number {
  let generation = start;
  return (floor) => {
    generation = Math.max(generation, floor) + 1;
    return generation;
  };
}

function NotesHarness({
  initialBody = "",
  meetingId = "meeting-a",
  nextWriteGeneration,
  onError = () => undefined,
  onSaved = () => undefined,
  saveNote,
  saveNoteOnTeardown,
  writerId = TEST_WRITER_ID,
}: {
  initialBody?: string;
  meetingId?: string;
  nextWriteGeneration?: (floor: number) => number;
  onError?: (error: Error, meetingId: string) => void;
  onSaved?: (savedNote: MeetingNote, meetingId: string) => void;
  saveNote: SaveMeetingNote;
  saveNoteOnTeardown?: SaveMeetingNote;
  writerId?: string;
}) {
  const generationRef = useRef(nextWriteGeneration ?? generationSequence());
  const autosave = useMeetingNotesAutosave({
    debounceMs: 700,
    initialBody,
    meetingId,
    nextWriteGeneration: generationRef.current,
    onError,
    onSaved,
    saveNote,
    saveNoteOnTeardown,
    writerId,
  });
  return (
    <LocaleProvider>
      <MeetingNotesEditor autosave={autosave} />
    </LocaleProvider>
  );
}

async function advanceAutosave(): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(700);
    await Promise.resolve();
  });
}

async function resolveSave(pending: Deferred<MeetingNote>, value: MeetingNote): Promise<void> {
  await act(async () => {
    pending.resolve(value);
    await pending.promise;
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("MeetingNotesEditor autosave", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    window.localStorage.removeItem(MEETING_NOTE_WRITER_STORAGE_KEY);
    window.localStorage.removeItem(MEETING_NOTE_GENERATION_STORAGE_KEY);
    vi.useFakeTimers();
  });

  afterEach(() => {
    setBackendBaseUrl("http://127.0.0.1:8765");
    setBackendSessionToken("");
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("serializes ordinary saves and coalesces pending edits", async () => {
    const firstSave = deferred<MeetingNote>();
    const secondSave = deferred<MeetingNote>();
    const pendingSaves = [firstSave, secondSave];
    let nextSave = 0;
    let activeSaves = 0;
    let maximumActiveSaves = 0;
    const saveNote = vi.fn<SaveMeetingNote>(async () => {
      const pending = pendingSaves[nextSave++];
      activeSaves += 1;
      maximumActiveSaves = Math.max(maximumActiveSaves, activeSaves);
      try {
        return await pending.promise;
      } finally {
        activeSaves -= 1;
      }
    });

    render(<NotesHarness saveNote={saveNote} />);
    const editor = screen.getByRole("textbox", { name: "Live notes" });

    fireEvent.change(editor, { target: { value: "first" } });
    await advanceAutosave();
    expect(saveNote).toHaveBeenCalledTimes(1);
    expect(saveNote).toHaveBeenLastCalledWith("meeting-a", "first", { writerId: TEST_WRITER_ID, generation: 1 });

    fireEvent.change(editor, { target: { value: "second" } });
    await advanceAutosave();
    fireEvent.change(editor, { target: { value: "third" } });
    await advanceAutosave();
    expect(saveNote).toHaveBeenCalledTimes(1);

    await resolveSave(firstSave, note("first", { generation: 1, applied: true }));
    expect(saveNote).toHaveBeenCalledTimes(2);
    expect(saveNote).toHaveBeenLastCalledWith("meeting-a", "third", { writerId: TEST_WRITER_ID, generation: 2 });
    expect(maximumActiveSaves).toBe(1);

    await resolveSave(secondSave, note("third", { generation: 2, applied: true }));
    expect(screen.getByText("Notes autosave and AI regeneration never overwrites them.")).toBeInTheDocument();
  });

  it("keeps B when pagehide B reaches the server before in-flight A", async () => {
    const saveA = deferred<MeetingNote>();
    const saveB = deferred<MeetingNote>();
    const saveNote = vi.fn<SaveMeetingNote>(() => saveA.promise);
    const teardownSave = vi.fn<SaveMeetingNote>(() => saveB.promise);
    const onSaved = vi.fn();

    render(<NotesHarness onSaved={onSaved} saveNote={saveNote} saveNoteOnTeardown={teardownSave} />);
    const editor = screen.getByRole("textbox", { name: "Live notes" });
    fireEvent.change(editor, { target: { value: "A" } });
    await advanceAutosave();
    fireEvent.change(editor, { target: { value: "B" } });

    window.dispatchEvent(new Event("pagehide"));
    expect(teardownSave).toHaveBeenCalledWith("meeting-a", "B", { writerId: TEST_WRITER_ID, generation: 2 });

    await resolveSave(saveB, note("B", { generation: 2, applied: true }));
    await resolveSave(saveA, note("B", { generation: 2, applied: false }));

    expect(onSaved).toHaveBeenCalledTimes(1);
    expect(onSaved).toHaveBeenLastCalledWith(expect.objectContaining({ body: "B", writeGeneration: 2 }), "meeting-a");
    expect(screen.getByText("Notes autosave and AI regeneration never overwrites them.")).toBeInTheDocument();
  });

  it("keeps B when in-flight A completes before pagehide B", async () => {
    const saveA = deferred<MeetingNote>();
    const saveB = deferred<MeetingNote>();
    const saveNote = vi.fn<SaveMeetingNote>(() => saveA.promise);
    const teardownSave = vi.fn<SaveMeetingNote>(() => saveB.promise);
    const onSaved = vi.fn();

    render(<NotesHarness onSaved={onSaved} saveNote={saveNote} saveNoteOnTeardown={teardownSave} />);
    const editor = screen.getByRole("textbox", { name: "Live notes" });
    fireEvent.change(editor, { target: { value: "A" } });
    await advanceAutosave();
    fireEvent.change(editor, { target: { value: "B" } });
    window.dispatchEvent(new Event("pagehide"));

    await resolveSave(saveA, note("A", { generation: 1, applied: true }));
    await resolveSave(saveB, note("B", { generation: 2, applied: true }));

    expect(onSaved.mock.calls.map(([saved]) => saved.body)).toEqual(["A", "B"]);
    expect(screen.getByText("Notes autosave and AI regeneration never overwrites them.")).toBeInTheDocument();
  });

  it("raises its generation floor and retries after a stale response", async () => {
    const retry = deferred<MeetingNote>();
    const saveNote = vi
      .fn<SaveMeetingNote>()
      .mockResolvedValueOnce(note("server A", { generation: 10, applied: false }))
      .mockImplementationOnce(() => retry.promise);

    render(<NotesHarness initialBody="server A" saveNote={saveNote} />);
    fireEvent.change(screen.getByRole("textbox", { name: "Live notes" }), { target: { value: "new B" } });
    await advanceAutosave();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(saveNote).toHaveBeenCalledTimes(2);
    expect(saveNote).toHaveBeenLastCalledWith("meeting-a", "new B", { writerId: TEST_WRITER_ID, generation: 11 });
    await resolveSave(retry, note("new B", { generation: 11, applied: true }));
  });

  it("keeps writer identity and generation durable across hook lifetimes", () => {
    const writer = persistentMeetingNoteWriterId();
    const firstGeneration = nextPersistentMeetingNoteGeneration();
    recordMeetingNoteGenerationFloor(firstGeneration + 50);
    const nextGeneration = nextPersistentMeetingNoteGeneration();

    expect(persistentMeetingNoteWriterId()).toBe(writer);
    expect(window.localStorage.getItem(MEETING_NOTE_WRITER_STORAGE_KEY)).toBe(writer);
    expect(nextGeneration).toBeGreaterThan(firstGeneration + 50);
    expect(Number(window.localStorage.getItem(MEETING_NOTE_GENERATION_STORAGE_KEY))).toBe(nextGeneration);
  });

  it("uses ordinary fetch normally and credential-safe keepalive only on pagehide", async () => {
    const privateToken = "private-session-token";
    setBackendBaseUrl("http://127.0.0.1:8765");
    setBackendSessionToken(privateToken);
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (_input, init) => {
      const payload = JSON.parse(String(init?.body)) as {
        body: string;
        writeGeneration: number;
      };
      return new Response(
        JSON.stringify(
          note(payload.body, {
            generation: payload.writeGeneration,
            applied: true,
          }),
        ),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<NotesHarness saveNote={saveWorkspaceMeetingNote} />);
    const editor = screen.getByRole("textbox", { name: "Live notes" });
    fireEvent.change(editor, { target: { value: "ordinary draft" } });
    await advanceAutosave();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    fireEvent.change(editor, { target: { value: "teardown draft" } });
    window.dispatchEvent(new Event("pagehide"));
    expect(fetchMock).toHaveBeenCalledTimes(2);

    const [normalUrl, normalInit] = fetchMock.mock.calls[0];
    const [teardownUrl, teardownInit] = fetchMock.mock.calls[1];
    expect(normalInit?.keepalive).toBe(false);
    expect(teardownInit?.keepalive).toBe(true);
    expect(String(normalUrl)).toBe("http://127.0.0.1:8765/api/meetings/meeting-a/notes");
    expect(String(teardownUrl)).not.toContain(privateToken);
    expect((teardownInit?.headers as Record<string, string>)["X-Scriber-Token"]).toBe(privateToken);
    expect(teardownInit?.referrerPolicy).toBe("no-referrer");
    expect(String(teardownInit?.body)).not.toContain(privateToken);
    expect(JSON.parse(String(teardownInit?.body))).toMatchObject({
      id: "workspace",
      body: "teardown draft",
      writerId: TEST_WRITER_ID,
      writeGeneration: 2,
    });
  });

  it("warns and falls back to ordinary fetch when serialized teardown exceeds the cap", () => {
    const escapedBody = "\u0000".repeat(10_000);
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify(
          note(escapedBody, {
            generation: 1,
            applied: true,
          }),
        ),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<NotesHarness saveNote={saveWorkspaceMeetingNote} />);
    fireEvent.change(screen.getByRole("textbox", { name: "Live notes" }), { target: { value: escapedBody } });

    expect(screen.getByRole("alert")).toHaveTextContent("This note is too large for a shutdown save.");
    window.dispatchEvent(new Event("pagehide"));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, requestInit] = fetchMock.mock.calls[0];
    expect(requestInit?.keepalive).toBe(false);
    expect(new TextEncoder().encode(String(requestInit?.body)).byteLength).toBeGreaterThan(
      MEETING_NOTE_KEEPALIVE_PAYLOAD_MAX_BYTES,
    );
  });

  it("keeps a failed draft dirty and retries the latest body", async () => {
    const failedSave = deferred<MeetingNote>();
    const retrySave = deferred<MeetingNote>();
    const saveNote = vi
      .fn<SaveMeetingNote>()
      .mockImplementationOnce(() => failedSave.promise)
      .mockImplementationOnce(() => retrySave.promise);

    render(<NotesHarness saveNote={saveNote} />);
    fireEvent.change(screen.getByRole("textbox", { name: "Live notes" }), { target: { value: "keep this draft" } });
    await advanceAutosave();
    await act(async () => {
      failedSave.reject(new Error("backend offline"));
      await failedSave.promise.catch(() => undefined);
      await Promise.resolve();
    });

    expect(screen.getByRole("alert")).toHaveTextContent("Note was not saved");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(saveNote).toHaveBeenLastCalledWith("meeting-a", "keep this draft", {
      writerId: TEST_WRITER_ID,
      generation: 2,
    });
    await resolveSave(retrySave, note("keep this draft", { generation: 2, applied: true }));
  });
});
