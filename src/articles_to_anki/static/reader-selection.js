import { workspace, status, selectionPopover, selectionTranslation, selectionAction, pageStates, highlightSignatures, state as readerState } from "./reader-state.js";
import { positionOverlay } from "./reader-overlays.js";
import { highlightSignature, roundCoordinate, makeUuid } from "./reader-highlights.js";

export function scheduleSelectionCapture(delay) {
  window.clearTimeout(readerState.selectionTimer);
  readerState.selectionTimer = window.setTimeout(captureSelection, delay);
}

export function captureSelection() {
  const selection = window.getSelection();
  const target = normalizeSelectedText(selection?.toString() || "");
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed || !isSelectableTarget(target)) return;
  const range = selection.getRangeAt(0);
  const node = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
    ? range.commonAncestorContainer
    : range.commonAncestorContainer.parentElement;
  const shell = node?.closest?.(".pdf-page");
  if (!shell) return;
  const pageNumber = Number(shell.dataset.page);
  const state = pageStates.get(pageNumber);
  if (!state?.textReady) return;
  const rects = selectionPdfRects(range, state);
  if (!rects.length) return;
  const sentence = findSentence(state.text || target, target, selectionHint(range, shell));
  const provisional = {
    id: makeUuid(),
    target,
    sentence,
    page: pageNumber,
    rects,
    translations: [],
    replacement: "",
    alternatives: [],
    status: "pending",
    error: null,
  };
  const signature = highlightSignature(provisional);
  if (highlightSignatures.has(signature)) {
    hideSelectionAction();
    return;
  }
  readerState.chosenSelection = provisional;
  selectionAction.textContent = `Добавить «${target}»`;
  selectionTranslation.textContent = "Ищем быстрый перевод…";
  selectionPopover.hidden = false;
  readerState.selectionAnchor = rectFromRange(range);
  positionOverlay(selectionPopover, readerState.selectionAnchor);
  void loadQuickTranslation(provisional);
}

export function rectFromRange(range) {
  const rect = range.getBoundingClientRect();
  return {left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width};
}

export function hideSelectionAction() {
  selectionPopover.hidden = true;
  readerState.chosenSelection = null;
  readerState.selectionAnchor = null;
}

export async function loadQuickTranslation(selection) {
  try {
    const url = new URL(workspace.dataset.quickTranslationUrl, window.location.origin);
    url.searchParams.set("word", selection.target);
    const response = await fetch(url);
    if (!response.ok) throw new Error("Быстрый перевод недоступен.");
    const result = await response.json();
    selection.quickTranslation = result.groups || [];
    if (readerState.chosenSelection !== selection || selectionPopover.hidden) return;
    selectionTranslation.textContent = formatQuickTranslation(selection.quickTranslation);
    positionOverlay(selectionPopover, readerState.selectionAnchor, true);
  } catch (error) {
    if (readerState.chosenSelection === selection && !selectionPopover.hidden) {
      selectionTranslation.textContent = "Быстрый перевод недоступен.";
      positionOverlay(selectionPopover, readerState.selectionAnchor, true);
    }
  }
}

export function formatQuickTranslation(groups) {
  const text = groups
    .filter((group) => Array.isArray(group.translations) && group.translations.length)
    .map((group) => `${group.part_of_speech ? `${group.part_of_speech}: ` : ""}${group.translations.join(", ")}`)
    .join(" · ");
  return text || "Быстрый перевод не найден.";
}

export function selectionPdfRects(range, state) {
  const pageBounds = state.shell.getBoundingClientRect();
  if (!pageBounds.width || !pageBounds.height) return [];
  const scaleX = state.viewport.width / pageBounds.width;
  const scaleY = state.viewport.height / pageBounds.height;
  const rectangles = [];
  for (const rectangle of range.getClientRects()) {
    const left = Math.max(pageBounds.left, rectangle.left);
    const top = Math.max(pageBounds.top, rectangle.top);
    const right = Math.min(pageBounds.right, rectangle.right);
    const bottom = Math.min(pageBounds.bottom, rectangle.bottom);
    if (right - left < 1 || bottom - top < 1) continue;
    const viewportPoints = [
      [(left - pageBounds.left) * scaleX, (top - pageBounds.top) * scaleY],
      [(right - pageBounds.left) * scaleX, (top - pageBounds.top) * scaleY],
      [(left - pageBounds.left) * scaleX, (bottom - pageBounds.top) * scaleY],
      [(right - pageBounds.left) * scaleX, (bottom - pageBounds.top) * scaleY],
    ];
    const pdfPoints = viewportPoints.map(([x, y]) => state.viewport.convertToPdfPoint(x, y));
    const xs = pdfPoints.map(([x]) => x);
    const ys = pdfPoints.map(([, y]) => y);
    rectangles.push({
      x1: roundCoordinate(Math.min(...xs)),
      y1: roundCoordinate(Math.min(...ys)),
      x2: roundCoordinate(Math.max(...xs)),
      y2: roundCoordinate(Math.max(...ys)),
    });
  }
  return rectangles;
}

export function selectionHint(range, shell) {
  const selectedSpan = (range.startContainer.nodeType === Node.ELEMENT_NODE
    ? range.startContainer
    : range.startContainer.parentElement)?.closest?.(".textLayer span:not(.markedContent)");
  if (!selectedSpan) return 0;
  let length = 0;
  for (const span of shell.querySelectorAll(".textLayer span:not(.markedContent)")) {
    if (span === selectedSpan) return length + Math.max(0, range.startOffset || 0);
    length += (span.textContent || "").length + 1;
  }
  return 0;
}

export function normalizeSelectedText(value) {
  return value
    .normalize("NFKC")
    .replace(/[-\u2010-\u2015\u2212\u2E3A\u2E3B\uFE58\uFE63\uFF0D\u00ad]\s*\r?\n\s*/g, "")
    .replace(/\u00ad/g, "")
    .replace(/[’‘ʼ＇]/g, "'")
    .replace(/[\p{Dash_Punctuation}\u2212]/gu, "-")
    .replace(/[^\p{L}\p{M}\p{N}'\-\s]/gu, " ")
    .toLocaleLowerCase()
    .replace(/\s+/g, " ")
    .replace(/-{2,}/g, "-")
    .replace(/'{2,}/g, "'")
    .trim()
    .replace(/^[-']+|[-']+$/g, "");
}

export function isSelectableTarget(value) {
  const words = value.split(" ");
  return words.length <= 3
    && words.every((word) => /^[\p{L}\p{N}_]+(?:['’\-][\p{L}\p{N}_]+)*$/u.test(word))
    && value.length <= 100;
}

export function findSentence(text, target, hint = 0) {
  const lower = text.toLocaleLowerCase();
  const needle = target.toLocaleLowerCase();
  const positions = [];
  let cursor = lower.indexOf(needle);
  while (cursor >= 0) {
    positions.push(cursor);
    cursor = lower.indexOf(needle, cursor + needle.length);
  }
  const index = positions.length
    ? positions.reduce((best, position) => (
      Math.abs(position - hint) < Math.abs(best - hint) ? position : best
    ), positions[0])
    : Math.max(0, hint);
  const before = text.slice(0, index);
  const left = Math.max(before.lastIndexOf(". "), before.lastIndexOf("? "), before.lastIndexOf("! "));
  const after = [text.indexOf(". ", index), text.indexOf("? ", index), text.indexOf("! ", index)]
    .filter((position) => position >= 0);
  const right = after.length ? Math.min(...after) + 1 : Math.min(text.length, index + 220);
  return text.slice(left >= 0 ? left + 2 : Math.max(0, index - 100), right).trim() || target;
}
