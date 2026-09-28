import { workspace, readToggle, readToggleLabel, pageStates, state as readerState } from "./reader-state.js";

export function scheduleProgressFromViewport() {
  window.clearTimeout(readerState.progressTimer);
  readerState.progressTimer = window.setTimeout(() => {
    const viewportMiddle = window.innerHeight / 2;
    let nearestPage = null;
    let nearestDistance = Infinity;
    for (const state of pageStates.values()) {
      const bounds = state.shell.getBoundingClientRect();
      if (bounds.bottom < 0 || bounds.top > window.innerHeight) continue;
      const distance = Math.abs((bounds.top + bounds.bottom) / 2 - viewportMiddle);
      if (distance < nearestDistance) {
        nearestDistance = distance;
        nearestPage = state.pageNumber;
      }
    }
    if (nearestPage !== null) queueProgressSave(nearestPage);
  }, 350);
}

export function queueProgressSave(pageNumber) {
  readerState.pendingProgressPage = pageNumber;
  if (!readerState.savingProgress) void saveProgress();
}

export async function saveProgress() {
  if (readerState.savingProgress || readerState.pendingProgressPage === null) return;
  const pageNumber = readerState.pendingProgressPage;
  if (pageNumber === readerState.savedProgressPage) return;
  readerState.savingProgress = true;
  try {
    const response = await fetch(workspace.dataset.progressUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": workspace.dataset.csrfToken,
      },
      body: JSON.stringify({page: pageNumber}),
    });
    if (!response.ok) throw new Error("Не удалось сохранить место чтения.");
    readerState.savedProgressPage = pageNumber;
  } catch (error) {
    console.error(error);
    if (readerState.pendingProgressPage === pageNumber) readerState.pendingProgressPage = null;
  } finally {
    readerState.savingProgress = false;
    if (readerState.pendingProgressPage !== readerState.savedProgressPage) void saveProgress();
  }
}

export async function saveReadState() {
  if (!readToggle) return;
  const wasRead = !readToggle.checked;
  readToggle.disabled = true;
  try {
    const response = await fetch(workspace.dataset.readUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": workspace.dataset.csrfToken,
      },
      body: JSON.stringify({read: readToggle.checked ? "1" : "0"}),
    });
    if (!response.ok) throw new Error("Не удалось изменить статус статьи.");
    const result = await response.json();
    readToggle.checked = result.read;
    readToggleLabel.textContent = result.read ? "Прочитано" : "Не прочитано";
  } catch (error) {
    console.error(error);
    readToggle.checked = wasRead;
    readToggleLabel.textContent = wasRead ? "Прочитано" : "Не прочитано";
  } finally {
    readToggle.disabled = false;
  }
}
