import { status, popover, popoverWord, popoverTranslation, selectionPopover, viewerBack, pageStates, highlights, state as readerState } from "./reader-state.js";
import { scheduleQualityRefresh } from "./reader-pdf.js";
import { formatQuickTranslation } from "./reader-selection.js";
import { saveHighlight } from "./reader-highlights.js";

export function visualBounds() {
  const viewport = window.visualViewport;
  const left = viewport?.offsetLeft || 0;
  const top = viewport?.offsetTop || 0;
  return {
    left,
    top,
    width: viewport?.width || window.innerWidth,
    height: viewport?.height || window.innerHeight,
  };
}

export function readerZoom() {
  const visualZoom = window.visualViewport?.scale || 1;
  const outerToInner = window.outerWidth && window.innerWidth
    ? window.outerWidth / window.innerWidth
    : 1;
  const desktopZoom = outerToInner > 1.25 ? outerToInner : 1;
  return Math.max(1, visualZoom, desktopZoom);
}

export function updateOverlayScale(element) {
  element.style.setProperty("--reader-control-scale", String(1 / readerZoom()));
}

export function updateReaderControlScales() {
  for (const element of [viewerBack, selectionPopover, popover]) {
    if (element) updateOverlayScale(element);
  }
}

export function positionOverlay(element, anchor, centered = false) {
  if (!anchor || element.hidden) return;
  const bounds = visualBounds();
  const gap = 10 / readerZoom();
  updateOverlayScale(element);
  element.style.maxWidth = `${Math.max(0, bounds.width - gap * 2)}px`;
  const width = element.offsetWidth;
  const height = element.offsetHeight;
  const preferredLeft = centered
    ? anchor.left + anchor.width / 2 - width / 2
    : anchor.left;
  const left = Math.min(
    bounds.left + bounds.width - width - gap,
    Math.max(bounds.left + gap, preferredLeft),
  );
  const above = anchor.top - height - 8;
  const below = anchor.bottom + 8;
  const top = above >= bounds.top + gap
    ? above
    : Math.min(bounds.top + bounds.height - height - gap, Math.max(bounds.top + gap, below));
  element.style.left = `${left}px`;
  element.style.top = `${top}px`;
}

export function showTranslation(event, highlight) {
  event.stopPropagation();
  if (highlight.status === "failed" && highlight.source !== "pdf_import") {
    void saveHighlight(highlight);
  }
  readerState.openHighlightId = highlight.id;
  popoverWord.textContent = highlight.target;
  renderPopoverText(highlight);
  popover.hidden = false;
  positionOverlay(popover, event.currentTarget.getBoundingClientRect());
}

export function renderPopoverText(highlight) {
  if (highlight.status === "ready") {
    popoverTranslation.textContent = highlight.translations.join(" · ");
  } else if (highlight.status === "failed") {
    popoverTranslation.textContent = highlight.error || "Перевод недоступен.";
  } else {
    const quick = formatQuickTranslation(highlight.quickTranslation || []);
    popoverTranslation.textContent = quick === "Быстрый перевод не найден."
      ? "Перевод готовится…"
      : `${quick} · Уточняем по контексту…`;
  }
}

export function hideTranslation(event) {
  if (event?.target?.closest?.(".word-highlight, .highlight-popover")) return;
  popover.hidden = true;
  readerState.openHighlightId = null;
}

export function repositionOpenOverlays() {
  if (!selectionPopover.hidden) positionOverlay(selectionPopover, readerState.selectionAnchor, true);
  if (!popover.hidden && readerState.openHighlightId) {
    const highlight = highlights.get(readerState.openHighlightId);
    const page = highlight && pageStates.get(highlight.page);
    const anchor = page?.shell.querySelector(".word-highlight");
    if (anchor) positionOverlay(popover, anchor.getBoundingClientRect());
  }
}

export function refreshZoomDependentUi() {
  updateReaderControlScales();
  scheduleQualityRefresh();
  repositionOpenOverlays();
}

