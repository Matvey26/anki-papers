const dialog = document.querySelector("#card-dialog");

document.querySelectorAll(".word").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelector("#target").value = button.dataset.word;
    document.querySelector("#sentence").value = button.dataset.sentence;
    document.querySelector("#dialog-word").textContent = button.dataset.word;
    document.querySelector("#dialog-sentence").textContent = button.dataset.sentence;
    document.querySelector("#translations").value = "";
    document.querySelector("#replacement").value = "";
    document.querySelector("#alternatives").value = "";
    document.querySelector("#enrich-error").textContent = "";
    dialog.showModal();
  });
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
