const deleteDialog = document.querySelector("#delete-article-dialog");
const deleteForm = document.querySelector("#delete-article-form");
const deleteName = document.querySelector("#delete-article-name");
const deleteConfirm = document.querySelector("[data-delete-confirm]");
const deleteCancel = document.querySelector("[data-delete-cancel]");
let deleteTimer = null;

function stopDeleteTimer() {
  window.clearInterval(deleteTimer);
  deleteTimer = null;
}

function openDeleteDialog(trigger) {
  stopDeleteTimer();
  deleteForm.action = trigger.dataset.deleteUrl;
  deleteName.textContent = trigger.dataset.documentName;
  deleteConfirm.disabled = true;
  let seconds = 3;
  deleteConfirm.textContent = `Удалить через ${seconds}`;
  deleteDialog.showModal();
  deleteTimer = window.setInterval(() => {
    seconds -= 1;
    if (seconds > 0) {
      deleteConfirm.textContent = `Удалить через ${seconds}`;
      return;
    }
    stopDeleteTimer();
    deleteConfirm.disabled = false;
    deleteConfirm.textContent = "Удалить навсегда";
  }, 1000);
}

for (const trigger of document.querySelectorAll(".delete-article-trigger")) {
  trigger.addEventListener("click", () => openDeleteDialog(trigger));
}

deleteCancel?.addEventListener("click", () => deleteDialog.close());
deleteDialog?.addEventListener("close", stopDeleteTimer);
deleteDialog?.addEventListener("click", (event) => {
  if (event.target === deleteDialog) deleteDialog.close();
});

const cardsToggle = document.querySelector("[data-cards-toggle]");
const cardsList = document.querySelector("#recent-card-list");
cardsToggle?.addEventListener("click", () => {
  const expanded = cardsToggle.getAttribute("aria-expanded") === "true";
  cardsToggle.setAttribute("aria-expanded", String(!expanded));
  cardsList.classList.toggle("is-collapsed", expanded);
  cardsToggle.textContent = expanded
    ? `Показать все · ${cardsList.children.length}`
    : "Свернуть";
});
