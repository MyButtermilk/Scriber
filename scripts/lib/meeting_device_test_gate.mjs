const REQUIRED_SOURCES = ["microphone", "system", "mic_clean"];

export function meetingDeviceTestPassed(result) {
  if (
    result?.available !== true ||
    result?.audioPersisted !== false ||
    result?.audioSentToProvider !== false ||
    typeof result?.sources !== "object" ||
    result.sources === null
  ) {
    return false;
  }

  return REQUIRED_SOURCES.every((source) => {
    const observed = result.sources[source];
    return (
      typeof observed === "object" &&
      observed !== null &&
      Number.isFinite(observed.frames) &&
      observed.frames > 0 &&
      Number.isFinite(observed.audioFrames) &&
      observed.audioFrames > 0 &&
      observed.active === true &&
      !observed.errorCode
    );
  });
}
