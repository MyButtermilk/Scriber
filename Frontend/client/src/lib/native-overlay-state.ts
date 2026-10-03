/** Order native events and the renderer-ready snapshot by the shell's lifecycle revision. */
export function createNativeOverlayStateGate(): (payload: { revision?: number }) => boolean {
  let newestRevision = -1;
  return ({ revision }) => {
    // Unversioned state is accepted only before the first versioned shell state.
    if (revision === undefined) return newestRevision < 0;
    if (!Number.isSafeInteger(revision) || revision < 0 || revision < newestRevision) return false;
    newestRevision = revision;
    // RMS events keep the current lifecycle revision and remain admissible.
    return true;
  };
}
