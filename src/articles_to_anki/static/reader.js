import * as pdfjs from "./vendor/pdfjs/pdf.min.mjs";
import { workspace, status, highlightDelete, selectionAction, readToggle, highlightSignatures, state as readerState } from "./reader-state.js";
import { openPdf } from "./reader-pdf.js";
import { scheduleProgressFromViewport, saveReadState } from "./reader-progress.js";
import { scheduleSelectionCapture, hideSelectionAction } from "./reader-selection.js";
import { hideTranslation, refreshZoomDependentUi, updateReaderControlScales } from "./reader-overlays.js";
import { saveHighlight, rememberHighlight, highlightSignature, drawPageHighlights, deleteOpenHighlight } from "./reader-highlights.js";

updateReaderControlScales();

pdfjs.GlobalWorkerOptions.workerSrc = workspace.dataset.workerUrl;

openPdf().catch((error) => {
  console.error(error);
  status.textContent = "Не удалось открыть PDF.";
});
document.addEventListener("selectionchange", () => scheduleSelectionCapture(300));
document.addEventListener("pointerup", () => scheduleSelectionCapture(70));
selectionAction.addEventListener("pointerdown", (event) => {
  event.preventDefault();
  event.stopPropagation();
});

selectionAction.addEventListener("click", (event) => {
  event.stopPropagation();
  const highlight = readerState.chosenSelection;
  hideSelectionAction();
  window.getSelection()?.removeAllRanges();
  if (!highlight || highlightSignatures.has(highlightSignature(highlight))) return;
  rememberHighlight(highlight);
  drawPageHighlights(highlight.page);
  void saveHighlight(highlight);
});
document.addEventListener("pointerdown", hideTranslation);
highlightDelete.addEventListener("click", (event) => {
  event.stopPropagation();
  void deleteOpenHighlight();
});
document.addEventListener("pointerdown", (event) => {
  if (!event.target.closest?.(".selection-action")) hideSelectionAction();
});
window.visualViewport?.addEventListener("resize", refreshZoomDependentUi, {passive: true});
window.visualViewport?.addEventListener("scroll", refreshZoomDependentUi, {passive: true});
window.addEventListener("resize", refreshZoomDependentUi, {passive: true});
window.addEventListener("scroll", () => {
  hideTranslation();
  hideSelectionAction();
  scheduleProgressFromViewport();
}, {passive: true});
readToggle?.addEventListener("change", () => {
  void saveReadState();
});
