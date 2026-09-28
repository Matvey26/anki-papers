export const workspace = document.querySelector("#pdf-workspace");
export const viewer = document.querySelector("#pdf-viewer");
export const status = document.querySelector("#pdf-status");
export const popover = document.querySelector("#highlight-popover");
export const popoverWord = document.querySelector("#highlight-word");
export const popoverTranslation = document.querySelector("#highlight-translation");
export const highlightDelete = document.querySelector("#highlight-delete");
export const selectionPopover = document.querySelector("#selection-popover");
export const selectionTranslation = document.querySelector("#selection-translation");
export const selectionAction = document.querySelector("#selection-action");
export const viewerBack = document.querySelector(".viewer-back");
export const readToggle = document.querySelector("#read-toggle");
export const readToggleLabel = document.querySelector("#read-toggle-label");
export const pageStates = new Map();
export const highlights = new Map();
export const highlightSignatures = new Set();
export const savingHighlights = new Set();
export const deletedHighlightIds = new Set();
export const visiblePages = new Set();
export const state = {
  pdfDocument: null,
  selectionTimer: null,
  qualityTimer: null,
  processingTimer: null,
  progressTimer: null,
  pendingProgressPage: null,
  savedProgressPage: null,
  savingProgress: false,
  openHighlightId: null,
  chosenSelection: null,
  selectionAnchor: null,
};
