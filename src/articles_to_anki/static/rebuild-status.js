const rebuildPanel = document.querySelector("[data-rebuild-job]");

function rebuildButton() {
  return document.querySelector("[data-rebuild-form] button");
}

let rebuildPollTimer = null;

function renderRebuildJob(job) {
  if (!job || job.id === "") {
    rebuildPanel?.remove();
    const button = rebuildButton();
    if (button) button.disabled = false;
    if (rebuildPollTimer) window.clearInterval(rebuildPollTimer);
    return;
  }
  const running = job.state === "queued" || job.state === "running";
  if (!rebuildPanel) return;
  rebuildPanel.textContent = "";
  const button = rebuildButton();
  if (button) button.disabled = running;

  if (running) {
    const label = document.createElement("div");
    label.className = "sync-progress-label";
    const stage = document.createElement("span");
    stage.textContent = job.stage || (job.state === "queued" ? "В очереди" : "Сборка идёт");
    const percent = document.createElement("span");
    percent.textContent = `${job.progress}%`;
    label.append(stage, percent);
    const bar = document.createElement("progress");
    bar.className = "sync-progress-track";
    bar.max = 100;
    bar.value = job.progress;
    const note = document.createElement("p");
    note.className = "field-help";
    note.textContent = "Можно закрыть или перезагрузить страницу — готовый файл появится здесь, когда сборка закончится.";
    rebuildPanel.append(label, bar, note);
    return;
  }

  if (rebuildPollTimer) window.clearInterval(rebuildPollTimer);
  if (job.state === "succeeded" && job.download_url) {
    const done = document.createElement("p");
    const strong = document.createElement("strong");
    strong.textContent = job.auto_upload
      ? "Готово — колода собрана и отправлена в AnkiWeb."
      : "Готово — файл собран из всех сохранённых слов.";
    done.append(strong);
    const extra = document.createElement("span");
    extra.textContent = job.auto_upload
      ? " Синхронизируйтесь в Anki Desktop, чтобы увидеть новую колоду."
      : " Скачайте его и импортируйте в Anki.";
    done.append(extra);
    rebuildPanel.append(done);
    if (job.deck_name) {
      const hint = document.createElement("p");
      hint.className = "field-help";
      hint.textContent = `В Anki появится НОВАЯ колода «${job.deck_name}» рядом со старыми — она не вольётся в «По умолчанию».`;
      rebuildPanel.append(hint);
    }
    const link = document.createElement("a");
    link.className = "primary";
    link.href = job.download_url;
    link.textContent = "Скачать APKG";
    rebuildPanel.append(link);
  } else if (job.state === "failed") {
    const error = document.createElement("p");
    error.className = "sync-error-detail";
    error.textContent = job.error || "Не удалось пересобрать колоду. Попробуйте ещё раз.";
    rebuildPanel.append(error);
  } else {
    rebuildPanel.remove();
  }
}

if (rebuildPanel) {
  const initial = {
    id: rebuildPanel.dataset.jobId,
    state: rebuildPanel.dataset.state,
    progress: Number(rebuildPanel.dataset.progress || 0),
    stage: rebuildPanel.dataset.stage,
    error: rebuildPanel.dataset.error || null,
    download_url: rebuildPanel.dataset.downloadUrl || null,
    deck_name: rebuildPanel.dataset.deckName || null,
    auto_upload: rebuildPanel.dataset.autoUpload === "1",
  };
  renderRebuildJob(initial);
  if (initial.state === "queued" || initial.state === "running") {
    const statusUrl = rebuildPanel.dataset.statusUrl;
    rebuildPollTimer = window.setInterval(async () => {
      try {
        const response = await fetch(statusUrl, {
          headers: { Accept: "application/json" },
        });
        if (!response.ok) return;
        const payload = await response.json();
        renderRebuildJob(payload.rebuild_job);
      } catch {
        /* network hiccup — retry on next tick */
      }
    }, 2000);
  }
}