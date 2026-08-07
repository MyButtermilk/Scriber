import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";

const AUTO_HIDE_DELAY_MS = 700;
const MIN_THUMB_SIZE_PX = 32;
const SCROLLABLE_EPSILON_PX = 1;

function cssPixel(value: number): string {
  return `${Math.round(value * 1_000) / 1_000}px`;
}

interface AppOverlayScrollbarProps {
  scrollContainerRef: RefObject<HTMLDivElement | null>;
}

interface ScrollMetrics {
  maximumScrollTop: number;
  thumbTravel: number;
}

interface DragState {
  pointerId: number;
  startClientY: number;
  startScrollTop: number;
}

export function AppOverlayScrollbar({ scrollContainerRef }: AppOverlayScrollbarProps) {
  const trackRef = useRef<HTMLDivElement>(null);
  const thumbRef = useRef<HTMLDivElement>(null);
  const hideTimerRef = useRef<number | null>(null);
  const animationFrameRef = useRef<number | null>(null);
  const metricsRef = useRef<ScrollMetrics>({ maximumScrollTop: 0, thumbTravel: 0 });
  const dragStateRef = useRef<DragState | null>(null);
  const scrollableRef = useRef(false);
  const [isScrollable, setIsScrollable] = useState(false);
  const [isVisible, setIsVisible] = useState(false);
  const [isDragging, setIsDragging] = useState(false);

  const clearHideTimer = useCallback(() => {
    if (hideTimerRef.current !== null) {
      window.clearTimeout(hideTimerRef.current);
      hideTimerRef.current = null;
    }
  }, []);

  const scheduleHide = useCallback(() => {
    clearHideTimer();
    hideTimerRef.current = window.setTimeout(() => {
      hideTimerRef.current = null;
      if (!dragStateRef.current) {
        setIsVisible(false);
      }
    }, AUTO_HIDE_DELAY_MS);
  }, [clearHideTimer]);

  const revealTemporarily = useCallback(() => {
    if (!scrollableRef.current || dragStateRef.current) {
      return;
    }
    setIsVisible(true);
    scheduleHide();
  }, [scheduleHide]);

  const updateThumb = useCallback(() => {
    const viewport = scrollContainerRef.current;
    const track = trackRef.current;
    const thumb = thumbRef.current;
    if (!viewport || !track || !thumb) {
      return;
    }

    const maximumScrollTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
    const nextScrollable = maximumScrollTop > SCROLLABLE_EPSILON_PX;
    scrollableRef.current = nextScrollable;
    setIsScrollable(nextScrollable);

    if (!nextScrollable || track.clientHeight <= 0) {
      metricsRef.current = { maximumScrollTop: 0, thumbTravel: 0 };
      thumb.style.height = "0px";
      thumb.style.transform = "translate3d(0, 0, 0)";
      clearHideTimer();
      setIsVisible(false);
      return;
    }

    const proportionalHeight = (viewport.clientHeight / viewport.scrollHeight) * track.clientHeight;
    const thumbHeight = Math.min(track.clientHeight, Math.max(MIN_THUMB_SIZE_PX, proportionalHeight));
    const thumbTravel = Math.max(0, track.clientHeight - thumbHeight);
    const clampedScrollTop = Math.min(maximumScrollTop, Math.max(0, viewport.scrollTop));
    const thumbOffset = maximumScrollTop > 0 ? (clampedScrollTop / maximumScrollTop) * thumbTravel : 0;

    metricsRef.current = { maximumScrollTop, thumbTravel };
    thumb.style.height = cssPixel(thumbHeight);
    thumb.style.transform = `translate3d(0, ${cssPixel(thumbOffset)}, 0)`;
  }, [clearHideTimer, scrollContainerRef]);

  const queueThumbUpdate = useCallback(() => {
    if (animationFrameRef.current !== null) {
      return;
    }
    animationFrameRef.current = window.requestAnimationFrame(() => {
      animationFrameRef.current = null;
      updateThumb();
    });
  }, [updateThumb]);

  useLayoutEffect(() => {
    updateThumb();
  }, [updateThumb]);

  useEffect(() => {
    const viewport = scrollContainerRef.current;
    if (!viewport) {
      return;
    }

    const handleScroll = () => {
      queueThumbUpdate();
      revealTemporarily();
    };
    const handleRevealActivity = () => revealTemporarily();
    const handleResize = () => queueThumbUpdate();

    viewport.addEventListener("scroll", handleScroll, { passive: true });
    viewport.addEventListener("pointerenter", handleRevealActivity, { passive: true });
    viewport.addEventListener("pointermove", handleRevealActivity, { passive: true });
    viewport.addEventListener("pointerleave", scheduleHide, { passive: true });
    viewport.addEventListener("focusin", handleRevealActivity);
    window.addEventListener("resize", handleResize, { passive: true });

    const resizeObserver =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => {
            queueThumbUpdate();
          });
    resizeObserver?.observe(viewport);
    if (viewport.firstElementChild instanceof HTMLElement) {
      resizeObserver?.observe(viewport.firstElementChild);
    }

    return () => {
      viewport.removeEventListener("scroll", handleScroll);
      viewport.removeEventListener("pointerenter", handleRevealActivity);
      viewport.removeEventListener("pointermove", handleRevealActivity);
      viewport.removeEventListener("pointerleave", scheduleHide);
      viewport.removeEventListener("focusin", handleRevealActivity);
      window.removeEventListener("resize", handleResize);
      resizeObserver?.disconnect();
    };
  }, [queueThumbUpdate, revealTemporarily, scheduleHide, scrollContainerRef]);

  useEffect(
    () => () => {
      clearHideTimer();
      if (animationFrameRef.current !== null) {
        window.cancelAnimationFrame(animationFrameRef.current);
      }
    },
    [clearHideTimer],
  );

  const finishDrag = useCallback(() => {
    if (!dragStateRef.current) {
      return;
    }
    dragStateRef.current = null;
    setIsDragging(false);
    setIsVisible(true);
    scheduleHide();
  }, [scheduleHide]);

  const handleThumbPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const viewport = scrollContainerRef.current;
      if (!viewport || !scrollableRef.current) {
        return;
      }

      event.preventDefault();
      clearHideTimer();
      dragStateRef.current = {
        pointerId: event.pointerId,
        startClientY: event.clientY,
        startScrollTop: viewport.scrollTop,
      };
      setIsVisible(true);
      setIsDragging(true);
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    [clearHideTimer, scrollContainerRef],
  );

  const handleThumbPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const viewport = scrollContainerRef.current;
      const dragState = dragStateRef.current;
      if (!viewport || !dragState || dragState.pointerId !== event.pointerId) {
        return;
      }

      event.preventDefault();
      const { maximumScrollTop, thumbTravel } = metricsRef.current;
      if (thumbTravel <= 0 || maximumScrollTop <= 0) {
        return;
      }
      const pointerDelta = event.clientY - dragState.startClientY;
      viewport.scrollTop = Math.min(
        maximumScrollTop,
        Math.max(0, dragState.startScrollTop + (pointerDelta / thumbTravel) * maximumScrollTop),
      );
      queueThumbUpdate();
    },
    [queueThumbUpdate, scrollContainerRef],
  );

  const handleThumbPointerUp = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (dragStateRef.current?.pointerId !== event.pointerId) {
        return;
      }
      event.currentTarget.releasePointerCapture?.(event.pointerId);
      finishDrag();
    },
    [finishDrag],
  );

  const handleThumbPointerEnter = useCallback(() => {
    if (isVisible) {
      clearHideTimer();
    }
  }, [clearHideTimer, isVisible]);

  return (
    <div
      ref={trackRef}
      className="app-overlay-scrollbar"
      data-app-overlay-scrollbar="true"
      data-dragging={isDragging ? "true" : "false"}
      data-scrollable={isScrollable ? "true" : "false"}
      data-visible={isScrollable && (isVisible || isDragging) ? "true" : "false"}
      aria-hidden="true"
    >
      <div
        ref={thumbRef}
        className="app-overlay-scrollbar-thumb"
        onLostPointerCapture={finishDrag}
        onPointerCancel={finishDrag}
        onPointerDown={handleThumbPointerDown}
        onPointerEnter={handleThumbPointerEnter}
        onPointerLeave={scheduleHide}
        onPointerMove={handleThumbPointerMove}
        onPointerUp={handleThumbPointerUp}
      />
    </div>
  );
}
