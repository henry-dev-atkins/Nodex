function ensureImportModalRoot() {
  let root = document.querySelector("#import-modal-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "import-modal-root";
    document.body.append(root);
  }
  return root;
}

function ensureContextMenuRoot() {
  let root = document.querySelector("#context-menu-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "context-menu-root";
    root.className = "context-menu-root";
    root.hidden = true;
    document.body.append(root);
  }
  return root;
}

function ensureMergeModePickerRoot() {
  let root = document.querySelector("#merge-mode-picker-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "merge-mode-picker-root";
    document.body.append(root);
  }
  return root;
}

export function renderAppShell(container) {
  container.innerHTML = `
    <aside class="sidebar">
      <div class="sidebar-header">
        <h1>Workspace</h1>
        <button id="new-thread-button" class="primary-button">New</button>
      </div>
      <div class="sidebar-body">
        <div class="sidebar-section-label">Conversations</div>
        <div id="thread-list" class="thread-list"></div>
      </div>
    </aside>
    <div id="sidebar-resizer" class="pane-resizer pane-resizer-vertical" aria-hidden="true"></div>

    <main class="main-shell">
      <header class="topbar">
        <div class="topbar-mainline">
          <h2 id="thread-title">No conversation</h2>
          <span id="thread-turn-label" class="topbar-turn">Start</span>
        </div>
        <div id="action-bar-root" class="topbar-action-slot"></div>
        <div class="topbar-actions">
          <div class="view-toggle" role="tablist" aria-label="View mode">
            <button id="focus-mode-button" class="ghost-button" type="button">Focus</button>
            <button id="map-mode-button" class="ghost-button" type="button">Map</button>
          </div>
          <span id="connection-status" class="status-dot is-idle" title="Connecting" aria-label="Connecting"></span>
        </div>
      </header>
      <div id="error-banner"></div>

      <section class="workspace-grid">
        <section class="primary-stage">
          <section id="focus-layout" class="focus-layout mode-stage">
            <div class="mode-stage-shell focus-stage-shell">
              <section class="transcript-panel transcript-panel-focus">
                <div id="focus-transcript-view" class="transcript-view"></div>
              </section>
            </div>
          </section>

          <section id="map-layout" class="map-layout mode-stage">
            <div class="mode-stage-shell map-stage-shell">
              <section class="graph-panel">
                <div id="graph-view" class="graph-view"></div>
              </section>
            </div>
          </section>
        </section>

        <aside class="inspector-rail">
          <div id="context-panel" class="inspector-slot"></div>
          <div id="compare-panel-root" class="inspector-slot"></div>
        </aside>
      </section>
    </main>
  `;

  return {
    app: container,
    mainShell: container.querySelector(".main-shell"),
    graphPanel: container.querySelector(".graph-panel"),
    threadList: container.querySelector("#thread-list"),
    graphView: container.querySelector("#graph-view"),
    errorBanner: container.querySelector("#error-banner"),
    actionBar: container.querySelector("#action-bar-root"),
    comparePanel: container.querySelector("#compare-panel-root"),
    contextPanel: container.querySelector("#context-panel"),
    focusTranscript: container.querySelector("#focus-transcript-view"),
    importModal: ensureImportModalRoot(),
    mergeModePicker: ensureMergeModePickerRoot(),
    contextMenu: ensureContextMenuRoot(),
    title: container.querySelector("#thread-title"),
    turnLabel: container.querySelector("#thread-turn-label"),
    status: container.querySelector("#connection-status"),
    focusModeButton: container.querySelector("#focus-mode-button"),
    mapModeButton: container.querySelector("#map-mode-button"),
    newThread: container.querySelector("#new-thread-button"),
    sidebarResizer: container.querySelector("#sidebar-resizer"),
  };
}
