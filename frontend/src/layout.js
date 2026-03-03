const LAYOUT_STORAGE_KEY = "codex-ui-layout-v3";
const DEFAULT_LAYOUT = {
  sidebarWidth: 232,
  mapGraphWidth: 560,
};

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function readLayoutState() {
  try {
    const raw = window.localStorage.getItem(LAYOUT_STORAGE_KEY);
    if (!raw) {
      return { ...DEFAULT_LAYOUT };
    }
    const parsed = JSON.parse(raw);
    return {
      sidebarWidth: Number(parsed.sidebarWidth) || DEFAULT_LAYOUT.sidebarWidth,
      mapGraphWidth: Number(parsed.mapGraphWidth) || DEFAULT_LAYOUT.mapGraphWidth,
    };
  } catch {
    return { ...DEFAULT_LAYOUT };
  }
}

export function createLayoutController(elements) {
  let layoutState = readLayoutState();

  function persistLayoutState() {
    window.localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(layoutState));
  }

  function clampLayoutState(nextState) {
    const appWidth = elements.app?.clientWidth || window.innerWidth;
    const mainWidth = elements.mainShell?.clientWidth || Math.max(window.innerWidth - 320, 720);
    return {
      sidebarWidth: clamp(nextState.sidebarWidth, 192, Math.max(224, Math.min(320, appWidth * 0.24))),
      mapGraphWidth: clamp(nextState.mapGraphWidth, 340, Math.max(420, Math.min(mainWidth * 0.68, mainWidth - 260))),
    };
  }

  function applyLayoutState(partial = {}, persist = false) {
    layoutState = clampLayoutState({ ...layoutState, ...partial });
    document.documentElement.style.setProperty("--sidebar-width", `${Math.round(layoutState.sidebarWidth)}px`);
    document.documentElement.style.setProperty("--map-graph-width", `${Math.round(layoutState.mapGraphWidth)}px`);
    if (persist) {
      persistLayoutState();
    }
  }

  function bindResizer(handle, onMove) {
    if (!handle) {
      return;
    }
    handle.addEventListener("pointerdown", (event) => {
      if (window.matchMedia("(max-width: 1080px)").matches) {
        return;
      }
      event.preventDefault();
      document.body.classList.add("is-resizing");
      const cleanup = () => {
        document.body.classList.remove("is-resizing");
        window.removeEventListener("pointermove", handleMove);
        window.removeEventListener("pointerup", handleUp);
        window.removeEventListener("pointercancel", handleUp);
      };
      const handleMove = (moveEvent) => {
        onMove(moveEvent);
      };
      const handleUp = () => {
        cleanup();
        persistLayoutState();
      };
      window.addEventListener("pointermove", handleMove);
      window.addEventListener("pointerup", handleUp);
      window.addEventListener("pointercancel", handleUp);
    });
  }

  function attachResizers() {
    bindResizer(elements.sidebarResizer, (event) => {
      const appRect = elements.app.getBoundingClientRect();
      applyLayoutState({ sidebarWidth: event.clientX - appRect.left });
    });

    bindResizer(elements.graphTranscriptResizer, (event) => {
      const panelRect = elements.graphPanel.getBoundingClientRect();
      applyLayoutState({ mapGraphWidth: event.clientX - panelRect.left });
    });
  }

  return {
    applyLayoutState,
    attachResizers,
  };
}
