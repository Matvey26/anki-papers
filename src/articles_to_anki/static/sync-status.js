const liveSyncStatus = document.querySelector("[data-sync-auto-refresh]");

if (liveSyncStatus) {
  window.setTimeout(() => {
    if (document.visibilityState === "visible") window.location.reload();
  }, 4000);
}

const disconnectForm = document.querySelector("[data-confirm-disconnect]");
disconnectForm?.addEventListener("submit", (event) => {
  if (!window.confirm("Отключить AnkiWeb? Карточки в AnkiWeb останутся, но реквизиты и локальное зеркало будут удалены.")) {
    event.preventDefault();
  }
});
