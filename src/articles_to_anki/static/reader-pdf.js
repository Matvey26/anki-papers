import { workspace, viewer, status, pageStates, highlights, visiblePages, state as readerState } from "./reader-state.js";
import { scheduleProgressFromViewport } from "./reader-progress.js";
import { readerZoom } from "./reader-overlays.js";
import { syncStoredHighlights, drawPageHighlights } from "./reader-highlights.js";
import * as pdfjs from "./vendor/pdfjs/pdf.min.mjs";

export async function openPdf() {
  const highlightsRequest = fetch(workspace.dataset.highlightsUrl)
    .then(async (response) => {
      if (!response.ok) throw new Error("Не удалось загрузить выделения.");
      return response.json();
    })
    .catch((error) => {
      console.error(error);
      return {highlights: []};
    });
  readerState.pdfDocument = await pdfjs.getDocument({url: workspace.dataset.pdfUrl}).promise;
  const initialPage = Math.min(
    readerState.pdfDocument.numPages,
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

  for (let pageNumber = 1; pageNumber <= readerState.pdfDocument.numPages; pageNumber += 1) {
    const page = await readerState.pdfDocument.getPage(pageNumber);
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
  syncStoredHighlights(stored);

  status.hidden = true;
  const targetPage = viewer.querySelector(`[data-page="${initialPage}"]`);
  targetPage?.scrollIntoView({block: "start"});
  window.setTimeout(scheduleProgressFromViewport, 100);
}

export async function renderPage(pageNumber) {
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

export async function renderTextLayer(state) {
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

export async function renderCanvas(state, requestedRatio = desiredRenderRatio()) {
  const maximumPixels = readerZoom() > 1 ? 64_000_000 : 24_000_000;
  const maximumByArea = Math.sqrt(maximumPixels / (state.viewport.width * state.viewport.height));
  const ratio = Math.max(1, Math.min(12, maximumByArea, requestedRatio));
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

export function desiredRenderRatio() {
  const zoom = window.visualViewport?.scale || 1;
  return Math.min(12, (window.devicePixelRatio || 1) * zoom);
}

export function scheduleQualityRefresh() {
  window.clearTimeout(readerState.qualityTimer);
  readerState.qualityTimer = window.setTimeout(() => {
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
