import { hideTranslation } from "./reader-overlays.js";
import { workspace, status, popover, popoverTranslation, highlightDelete, pageStates, highlights, highlightSignatures, savingHighlights, deletedHighlightIds, state as readerState } from "./reader-state.js";
import { showTranslation, renderPopoverText } from "./reader-overlays.js";

export async function saveHighlight(highlight) {
  if (savingHighlights.has(highlight.id)) return;
  savingHighlights.add(highlight.id);
  let activeHighlight = highlight;
  highlight.status = "pending";
  highlight.error = null;
  drawPageHighlights(highlight.page);
  try {
    const response = await fetch(workspace.dataset.highlightsUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": workspace.dataset.csrfToken,
      },
      body: JSON.stringify({
        id: highlight.id,
        target: highlight.target,
        sentence: highlight.sentence,
        page: highlight.page,
        rects: highlight.rects,
      }),
    });
    const result = await response.json();
    if (deletedHighlightIds.has(highlight.id)) return;
    if (result.discarded_highlight_id) {
      deletedHighlightIds.add(highlight.id);
      highlights.delete(highlight.id);
      highlightSignatures.delete(highlightSignature(highlight));
      if (readerState.openHighlightId === highlight.id) hideTranslation();
      return;
    }
    if (result.highlight) {
      if (result.highlight.id !== highlight.id) {
        highlights.delete(highlight.id);
        highlightSignatures.delete(highlightSignature(highlight));
        if (readerState.openHighlightId === highlight.id) readerState.openHighlightId = result.highlight.id;
      }
      activeHighlight = {...result.highlight, quickTranslation: highlight.quickTranslation || []};
      rememberHighlight(activeHighlight);
    }
    if (!response.ok) throw new Error(result.error || "Автоперевод недоступен.");
  } catch (error) {
    if (deletedHighlightIds.has(highlight.id)) return;
    const current = highlights.get(activeHighlight.id) || activeHighlight;
    if (current.status === "pending") {
      current.status = "failed";
      current.error = error.message;
      rememberHighlight(current);
    }
  } finally {
    savingHighlights.delete(highlight.id);
    drawPageHighlights(highlight.page);
    scheduleProcessingPoll(null);
  }
}

export function syncStoredHighlights(payload) {
  const importedIds = new Set(
    payload.highlights
      .filter((highlight) => highlight.source === "pdf_import")
      .map((highlight) => highlight.id),
  );
  for (const [id, highlight] of highlights) {
    if (highlight.source === "pdf_import" && !importedIds.has(id)) {
      highlights.delete(id);
      highlightSignatures.delete(highlightSignature(highlight));
    }
  }
  for (const highlight of payload.highlights) {
    const previous = highlights.get(highlight.id);
    if (previous?.quickTranslation) highlight.quickTranslation = previous.quickTranslation;
    rememberHighlight(highlight);
  }
  for (const pageNumber of pageStates.keys()) drawPageHighlights(pageNumber);
  scheduleProcessingPoll(payload.processing_status);
}

export function scheduleProcessingPoll(processingStatus) {
  window.clearTimeout(readerState.processingTimer);
  const hasPendingReaderHighlight = [...highlights.values()].some(
    (highlight) => highlight.status === "pending" && highlight.source !== "pdf_import",
  );
  if (!["queued", "processing"].includes(processingStatus) && !hasPendingReaderHighlight) return;
  readerState.processingTimer = window.setTimeout(async () => {
    try {
      const response = await fetch(workspace.dataset.highlightsUrl);
      if (!response.ok) throw new Error("Не удалось обновить хайлайты.");
      syncStoredHighlights(await response.json());
    } catch (error) {
      console.error(error);
      scheduleProcessingPoll("processing");
    }
  }, 2500);
}

export function rememberHighlight(highlight) {
  if (deletedHighlightIds.has(highlight.id)) return;
  const previous = highlights.get(highlight.id);
  if (previous) highlightSignatures.delete(highlightSignature(previous));
  highlights.set(highlight.id, highlight);
  highlightSignatures.add(highlightSignature(highlight));
  if (readerState.openHighlightId === highlight.id && !popover.hidden) renderPopoverText(highlight);
}

export function highlightSignature(highlight) {
  const coordinates = highlight.rects.map((rectangle) => [
    rectangle.x1,
    rectangle.y1,
    rectangle.x2,
    rectangle.y2,
  ]);
  return `${highlight.page}:${highlight.target.toLocaleLowerCase()}:${JSON.stringify(coordinates)}`;
}

export function drawPageHighlights(pageNumber) {
  const state = pageStates.get(pageNumber);
  if (!state) return;
  const layer = state.shell.querySelector(".highlight-layer");
  layer.replaceChildren();
  for (const highlight of highlights.values()) {
    if (highlight.page !== pageNumber) continue;
    for (const rectangle of highlight.rects) {
      const bounds = pdfRectToViewport(rectangle, state.viewport);
      const button = document.createElement("button");
      button.type = "button";
      button.className = `word-highlight is-${highlight.status}`;
      button.style.left = `${bounds.left}px`;
      button.style.top = `${bounds.top}px`;
      button.style.width = `${bounds.width}px`;
      button.style.height = `${bounds.height}px`;
      button.setAttribute("aria-label", `Перевод слова ${highlight.target}`);
      button.addEventListener("click", (event) => showTranslation(event, highlight));
      layer.append(button);
    }
  }
}

export function pdfRectToViewport(rectangle, viewport) {
  const points = [
    viewport.convertToViewportPoint(rectangle.x1, rectangle.y1),
    viewport.convertToViewportPoint(rectangle.x2, rectangle.y1),
    viewport.convertToViewportPoint(rectangle.x1, rectangle.y2),
    viewport.convertToViewportPoint(rectangle.x2, rectangle.y2),
  ];
  const xs = points.map(([x]) => x);
  const ys = points.map(([, y]) => y);
  const left = Math.min(...xs);
  const top = Math.min(...ys);
  return {
    left,
    top,
    width: Math.max(...xs) - left,
    height: Math.max(...ys) - top,
  };
}

export async function deleteOpenHighlight() {
  const highlight = highlights.get(readerState.openHighlightId);
  if (!highlight) return;
  highlightDelete.disabled = true;
  highlightDelete.textContent = "Удаляем…";
  try {
    const response = await fetch(
      `${workspace.dataset.highlightsUrl}/${encodeURIComponent(highlight.id)}`,
      {
        method: "DELETE",
        headers: {"X-CSRF-Token": workspace.dataset.csrfToken},
      },
    );
    if (!response.ok) throw new Error("Не удалось удалить выделение.");
    deletedHighlightIds.add(highlight.id);
    highlights.delete(highlight.id);
    highlightSignatures.delete(highlightSignature(highlight));
    popover.hidden = true;
    readerState.openHighlightId = null;
    drawPageHighlights(highlight.page);
  } catch (error) {
    popoverTranslation.textContent = error.message;
  } finally {
    highlightDelete.disabled = false;
    highlightDelete.textContent = "Удалить выделение";
  }
}

export function roundCoordinate(value) {
  return Math.round(value * 1000) / 1000;
}

export function makeUuid() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof crypto.getRandomValues === "function") crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.random() * 256;
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}
