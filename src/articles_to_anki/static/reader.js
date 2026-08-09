import * as pdfjs from "./vendor/pdfjs/pdf.min.mjs";

const workspace = document.querySelector("#pdf-workspace");
const viewer = document.querySelector("#pdf-viewer");
const status = document.querySelector("#pdf-status");
const popover = document.querySelector("#highlight-popover");
const popoverWord = document.querySelector("#highlight-word");
const popoverTranslation = document.querySelector("#highlight-translation");
const pageStates = new Map();
const highlights = new Map();
const highlightSignatures = new Set();
const visiblePages = new Set();
let pdfDocument = null;
let selectionTimer = null;
let qualityTimer = null;
let openHighlightId = null;

pdfjs.GlobalWorkerOptions.workerSrc = workspace.dataset.workerUrl;

openPdf().catch((error) => {
  console.error(error);
  status.textContent = "Не удалось открыть PDF.";
});

async function openPdf() {
  const highlightsRequest = fetch(workspace.dataset.highlightsUrl)
    .then(async (response) => {
      if (!response.ok) throw new Error("Не удалось загрузить выделения.");
      return response.json();
    })
    .catch((error) => {
      console.error(error);
      return {highlights: []};
    });
  pdfDocument = await pdfjs.getDocument({url: workspace.dataset.pdfUrl}).promise;
  const initialPage = Math.min(
    pdfDocument.numPages,
    Math.max(1, Number(workspace.dataset.initialPage) || 1),
  );
  const availableWidth = Math.max(280, Math.min(1100, viewer.clientWidth - 24));
  const preloadObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) void renderPage(Number(entry.target.dataset.page));
      }
    },
    {rootMargin: "900px 0px"},
  );
  const visibilityObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const pageNumber = Number(entry.target.dataset.page);
      if (entry.isIntersecting) visiblePages.add(pageNumber);
      else visiblePages.delete(pageNumber);
    }
  });

  for (let pageNumber = 1; pageNumber <= pdfDocument.numPages; pageNumber += 1) {
    const page = await pdfDocument.getPage(pageNumber);
    const base = page.getViewport({scale: 1});
    const scale = availableWidth / base.width;
    const viewport = page.getViewport({scale});
    const shell = document.createElement("section");
    shell.className = "pdf-page";
    shell.dataset.page = String(pageNumber);
    shell.setAttribute("aria-label", `Страница ${pageNumber}`);
    shell.style.width = `${viewport.width}px`;
    shell.style.height = `${viewport.height}px`;
    shell.style.setProperty("--total-scale-factor", String(scale));
    shell.innerHTML = [
      "<canvas></canvas>",
      '<div class="highlight-layer"></div>',
      '<div class="textLayer"></div>',
      `<span class="page-number">${pageNumber}</span>`,
    ].join("");
    pageStates.set(pageNumber, {
      page,
      pageNumber,
      shell,
      viewport,
      text: "",
      textReady: false,
      loadPromise: null,
      renderPromise: null,
      renderedRatio: 0,
      requestedRatio: 0,
    });
    viewer.append(shell);
    preloadObserver.observe(shell);
    visibilityObserver.observe(shell);
  }

  const stored = await highlightsRequest;
  for (const highlight of stored.highlights) rememberHighlight(highlight);
  for (const pageNumber of pageStates.keys()) drawPageHighlights(pageNumber);
  for (const highlight of highlights.values()) {
    if (highlight.status === "pending") void saveHighlight(highlight);
  }

  status.hidden = true;
  const targetPage = viewer.querySelector(`[data-page="${initialPage}"]`);
  targetPage?.scrollIntoView({block: "start"});
}

async function renderPage(pageNumber) {
  const state = pageStates.get(pageNumber);
  if (!state) return;
  if (state.loadPromise) return state.loadPromise;
  state.loadPromise = (async () => {
    state.shell.classList.add("loading");
    try {
      await Promise.all([renderCanvas(state), renderTextLayer(state)]);
      state.shell.classList.remove("loading", "failed");
      state.shell.classList.add("ready");
      drawPageHighlights(pageNumber);
    } catch (error) {
      state.shell.classList.remove("loading");
      state.shell.classList.add("failed");
      console.error(error);
    } finally {
      state.loadPromise = null;
    }
  })();
  return state.loadPromise;
}

async function renderTextLayer(state) {
  if (state.textReady) return;
  const element = state.shell.querySelector(".textLayer");
  element.replaceChildren();
  const textLayer = new pdfjs.TextLayer({
    textContentSource: state.page.streamTextContent({
      includeMarkedContent: true,
      disableNormalization: true,
    }),
    container: element,
    viewport: state.viewport,
  });
  await textLayer.render();
  state.text = textLayer.textContentItemsStr.join(" ").replace(/\s+/g, " ").trim();
  state.textReady = true;
}

async function renderCanvas(state, requestedRatio = desiredRenderRatio()) {
  const maximumByArea = Math.sqrt(24_000_000 / (state.viewport.width * state.viewport.height));
  const ratio = Math.max(1, Math.min(6, maximumByArea, requestedRatio));
  if (state.renderedRatio >= ratio - 0.15) return;
  state.requestedRatio = Math.max(state.requestedRatio, ratio);
  if (state.renderPromise) return state.renderPromise;

  state.renderPromise = (async () => {
    const targetRatio = state.requestedRatio;
    state.requestedRatio = 0;
    const canvas = document.createElement("canvas");
    canvas.width = Math.ceil(state.viewport.width * targetRatio);
    canvas.height = Math.ceil(state.viewport.height * targetRatio);
    canvas.style.width = `${state.viewport.width}px`;
    canvas.style.height = `${state.viewport.height}px`;
    const context = canvas.getContext("2d", {alpha: false});
    await state.page.render({
      canvas,
      canvasContext: context,
      viewport: state.viewport,
      transform: targetRatio === 1 ? null : [targetRatio, 0, 0, targetRatio, 0, 0],
    }).promise;
    state.shell.querySelector("canvas").replaceWith(canvas);
    state.renderedRatio = targetRatio;
  })();
  try {
    await state.renderPromise;
  } finally {
    state.renderPromise = null;
  }
  if (state.requestedRatio > state.renderedRatio + 0.15) {
    return renderCanvas(state, state.requestedRatio);
  }
}

function desiredRenderRatio() {
  const zoom = window.visualViewport?.scale || 1;
  return Math.min(6, (window.devicePixelRatio || 1) * zoom);
}

function scheduleQualityRefresh() {
  window.clearTimeout(qualityTimer);
  qualityTimer = window.setTimeout(() => {
    const ratio = desiredRenderRatio();
    for (const pageNumber of visiblePages) {
      const state = pageStates.get(pageNumber);
      if (!state) continue;
      if (!state.textReady) {
        void renderPage(pageNumber).then(() => renderCanvas(state, ratio)).catch(console.error);
      }
      else void renderCanvas(state, ratio).catch(console.error);
    }
  }, 220);
}

window.visualViewport?.addEventListener("resize", scheduleQualityRefresh, {passive: true});
window.visualViewport?.addEventListener("scroll", scheduleQualityRefresh, {passive: true});
window.addEventListener("resize", scheduleQualityRefresh, {passive: true});

document.addEventListener("selectionchange", () => scheduleSelectionCapture(300));
document.addEventListener("pointerup", () => scheduleSelectionCapture(70));

function scheduleSelectionCapture(delay) {
  window.clearTimeout(selectionTimer);
  selectionTimer = window.setTimeout(captureSelection, delay);
}

function captureSelection() {
  const selection = window.getSelection();
  const target = selection?.toString().trim() || "";
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed || !isSingleWord(target)) return;
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
  selection.removeAllRanges();
  if (highlightSignatures.has(signature)) return;
  rememberHighlight(provisional);
  drawPageHighlights(pageNumber);
  void saveHighlight(provisional);
}

function selectionPdfRects(range, state) {
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

function selectionHint(range, shell) {
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

function isSingleWord(value) {
  return /^[\p{L}\p{N}_]+(?:['’\-][\p{L}\p{N}_]+)*$/u.test(value) && value.length <= 100;
}

function findSentence(text, target, hint = 0) {
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

async function saveHighlight(highlight) {
  let activeHighlight = highlight;
  highlight.status = "pending";
  highlight.error = null;
  drawPageHighlights(highlight.page);
  try {
    const response = await fetch(workspace.dataset.highlightsUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": window.ANKI_PAPERS_CSRF,
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
    if (result.highlight) {
      if (result.highlight.id !== highlight.id) {
        highlights.delete(highlight.id);
        highlightSignatures.delete(highlightSignature(highlight));
        if (openHighlightId === highlight.id) openHighlightId = result.highlight.id;
      }
      activeHighlight = result.highlight;
      rememberHighlight(result.highlight);
    }
    if (!response.ok) throw new Error(result.error || "Автоперевод недоступен.");
  } catch (error) {
    const current = highlights.get(activeHighlight.id) || activeHighlight;
    if (current.status === "pending") {
      current.status = "failed";
      current.error = error.message;
      rememberHighlight(current);
    }
  } finally {
    drawPageHighlights(highlight.page);
  }
}

function rememberHighlight(highlight) {
  const previous = highlights.get(highlight.id);
  if (previous) highlightSignatures.delete(highlightSignature(previous));
  highlights.set(highlight.id, highlight);
  highlightSignatures.add(highlightSignature(highlight));
  if (openHighlightId === highlight.id && !popover.hidden) renderPopoverText(highlight);
}

function highlightSignature(highlight) {
  const coordinates = highlight.rects.map((rectangle) => [
    rectangle.x1,
    rectangle.y1,
    rectangle.x2,
    rectangle.y2,
  ]);
  return `${highlight.page}:${highlight.target.toLocaleLowerCase()}:${JSON.stringify(coordinates)}`;
}

function drawPageHighlights(pageNumber) {
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

function pdfRectToViewport(rectangle, viewport) {
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

function showTranslation(event, highlight) {
  event.stopPropagation();
  if (highlight.status === "failed") void saveHighlight(highlight);
  openHighlightId = highlight.id;
  popoverWord.textContent = highlight.target;
  renderPopoverText(highlight);
  popover.hidden = false;
  const anchor = event.currentTarget.getBoundingClientRect();
  const left = Math.min(
    window.innerWidth - popover.offsetWidth - 10,
    Math.max(10, anchor.left),
  );
  const top = anchor.top > popover.offsetHeight + 16
    ? anchor.top - popover.offsetHeight - 8
    : anchor.bottom + 8;
  popover.style.left = `${left}px`;
  popover.style.top = `${Math.max(10, top)}px`;
}

function renderPopoverText(highlight) {
  if (highlight.status === "ready") {
    popoverTranslation.textContent = highlight.translations.join(" · ");
  } else if (highlight.status === "failed") {
    popoverTranslation.textContent = "Повторяем перевод…";
  } else {
    popoverTranslation.textContent = "Перевод готовится…";
  }
}

function hideTranslation(event) {
  if (event?.target?.closest?.(".word-highlight, .highlight-popover")) return;
  popover.hidden = true;
  openHighlightId = null;
}

function roundCoordinate(value) {
  return Math.round(value * 1000) / 1000;
}

function makeUuid() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof crypto.getRandomValues === "function") crypto.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.random() * 256;
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

document.addEventListener("pointerdown", hideTranslation);
window.addEventListener("scroll", () => hideTranslation(), {passive: true});
