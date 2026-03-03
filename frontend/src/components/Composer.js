export function updateComposer(state, elements, handlers) {
  const threadId = state.selectedThreadId;
  const turns = threadId ? state.turnsByThread[threadId] || [] : [];
  const busy = turns.some((turn) => turn.status === "running" || turn.status === "submitted" || turn.status === "inProgress");
  elements.submit.disabled = !threadId || busy;
  elements.fork.disabled = !threadId;
  elements.importButton.disabled = !threadId || !Object.keys(state.importSelection).some((key) => key.startsWith(`${threadId}:`) && state.importSelection[key]);
  elements.title.textContent = threadId ? state.threads[threadId]?.title || threadId : "No thread selected";

  elements.form.onsubmit = async (event) => {
    event.preventDefault();
    if (!threadId || !elements.input.value.trim()) {
      return;
    }
    await handlers.onSubmit(threadId, elements.input.value.trim());
    elements.input.value = "";
  };
}
