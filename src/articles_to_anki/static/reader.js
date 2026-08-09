import * as pdfjs from "./vendor/pdfjs/pdf.min.mjs";

const workspace = document.querySelector("#pdf-workspace");
const viewer = document.querySelector("#pdf-viewer");
const status = document.querySelector("#pdf-status");
const selectionAction = document.querySelector("#selection-action");
const dialog = document.querySelector("#card-dialog");
const pageTexts = new Map();
const renderedPages = new Set();
let chosenWord = null;

pdfjs.GlobalWorkerOptions.workerSrc = workspace.dataset.workerUrl;

openPdf().catch((error) => {
  console.error(error);
  status.textContent = "Не удалось открыть PDF.";
});

async function openPdf() {
  const pdf = await pdfjs.getDocument({url: workspace.dataset.pdfUrl}).promise;
  const initialPage = Math.min(pdf.numPages, Math.max(1, Number(workspace.dataset.initialPage) || 1));
  const availableWidth = Math.max(280, Math.min(1100, viewer.clientWidth - 24));
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) void renderPage(pdf, Number(entry.target.dataset.page));
      }
    },
    {rootMargin: "900px 0px"},
  );

  for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
    const page = await pdf.getPage(pageNumber);
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
    shell.innerHTML = `<canvas></canvas><div class="textLayer"></div><span class="page-number">${pageNumber}</span>`;
    viewer.append(shell);
    observer.observe(shell);
  }

  status.hidden = true;
  const targetPage = viewer.querySelector(`[data-page="${initialPage}"]`);
  targetPage?.scrollIntoView({block: "start"});
}

async function renderPage(pdf, pageNumber) {
  if (renderedPages.has(pageNumber)) return;
  renderedPages.add(pageNumber);
  const shell = viewer.querySelector(`[data-page="${pageNumber}"]`);
  if (!shell) return;
  shell.classList.add("loading");
  try {
    const page = await pdf.getPage(pageNumber);
    const base = page.getViewport({scale: 1});
    const scale = shell.clientWidth / base.width;
    const viewport = page.getViewport({scale});
    const canvas = shell.querySelector("canvas");
    const textLayerElement = shell.querySelector(".textLayer");
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(viewport.width * ratio);
    canvas.height = Math.floor(viewport.height * ratio);
    canvas.style.width = `${viewport.width}px`;
    canvas.style.height = `${viewport.height}px`;
    const context = canvas.getContext("2d", {alpha: false});
    const textLayer = new pdfjs.TextLayer({
      textContentSource: page.streamTextContent({includeMarkedContent: true, disableNormalization: true}),
      container: textLayerElement,
      viewport,
    });
    await Promise.all([
      page.render({
        canvas,
        canvasContext: context,
        viewport,
        transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0],
      }).promise,
      textLayer.render(),
    ]);
    pageTexts.set(pageNumber, textLayer.textContentItemsStr.join(" ").replace(/\s+/g, " ").trim());
    shell.classList.remove("loading");
    shell.classList.add("ready");
  } catch (error) {
    renderedPages.delete(pageNumber);
    shell.classList.remove("loading");
    shell.classList.add("failed");
    console.error(error);
  }
}

document.addEventListener("selectionchange", () => {
  window.clearTimeout(document.selectionTimer);
  document.selectionTimer = window.setTimeout(readSelection, 120);
});
document.addEventListener("pointerup", () => window.setTimeout(readSelection, 0));
window.addEventListener("scroll", hideSelectionAction, {passive: true});

function readSelection() {
  const selection = window.getSelection();
  const value = selection?.toString().trim() || "";
  if (!selection || selection.rangeCount !== 1 || !isSingleWord(value)) {
    hideSelectionAction();
    return;
  }
  const range = selection.getRangeAt(0);
  const node = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
    ? range.commonAncestorContainer
    : range.commonAncestorContainer.parentElement;
  const page = node?.closest?.(".pdf-page");
  if (!page) {
    hideSelectionAction();
    return;
  }
  const pageNumber = Number(page.dataset.page);
  const rectangle = range.getBoundingClientRect();
  chosenWord = {
    target: value,
    sentence: findSentence(pageTexts.get(pageNumber) || value, value),
    page: pageNumber,
  };
  selectionAction.textContent = `Добавить «${value}»`;
  selectionAction.hidden = false;
  const left = Math.min(window.innerWidth - selectionAction.offsetWidth - 10, Math.max(10, rectangle.left));
  const top = Math.max(10, rectangle.top - selectionAction.offsetHeight - 9);
  selectionAction.style.left = `${left}px`;
  selectionAction.style.top = `${top}px`;
}

function hideSelectionAction() {
  selectionAction.hidden = true;
}

function isSingleWord(value) {
  return /^[\p{L}\p{N}]+(?:['’\-][\p{L}\p{N}]+)*$/u.test(value);
}

function findSentence(text, target) {
  const lower = text.toLocaleLowerCase();
  const index = lower.indexOf(target.toLocaleLowerCase());
  if (index < 0) return target;
  const before = text.slice(0, index);
  const left = Math.max(before.lastIndexOf(". "), before.lastIndexOf("? "), before.lastIndexOf("! "));
  const after = [text.indexOf(". ", index), text.indexOf("? ", index), text.indexOf("! ", index)]
    .filter((position) => position >= 0);
  const right = after.length ? Math.min(...after) + 1 : Math.min(text.length, index + 220);
  return text.slice(left >= 0 ? left + 2 : Math.max(0, index - 100), right).trim();
}

selectionAction.addEventListener("pointerdown", (event) => event.preventDefault());
selectionAction.addEventListener("click", () => {
  if (!chosenWord) return;
  document.querySelector("#target").value = chosenWord.target;
  document.querySelector("#sentence").value = chosenWord.sentence;
  document.querySelector("#page").value = String(chosenWord.page);
  document.querySelector("#dialog-word").textContent = chosenWord.target;
  document.querySelector("#dialog-sentence").textContent = chosenWord.sentence;
  document.querySelector("#translations").value = "";
  document.querySelector("#replacement").value = "";
  document.querySelector("#alternatives").value = "";
  document.querySelector("#enrich-error").textContent = "";
  hideSelectionAction();
  dialog.showModal();
});

document.querySelector("[data-close]")?.addEventListener("click", () => dialog.close());

document.querySelector("#enrich")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const error = document.querySelector("#enrich-error");
  button.disabled = true;
  button.textContent = "Заполняем…";
  error.textContent = "";
  try {
    const response = await fetch("/api/enrich", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": window.ANKI_PAPERS_CSRF},
      body: JSON.stringify({
        target: document.querySelector("#target").value,
        sentence: document.querySelector("#sentence").value,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Не удалось заполнить.");
    document.querySelector("#translations").value = result.translations.join(", ");
    document.querySelector("#replacement").value = result.replacement;
    document.querySelector("#alternatives").value = result.alternatives.join(", ");
  } catch (caught) {
    error.textContent = caught.message;
  } finally {
    button.disabled = false;
    button.textContent = "Заполнить автоматически";
  }
});
