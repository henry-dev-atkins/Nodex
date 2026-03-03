export function connectEventStream(token, store) {
  let socket = null;
  let reconnectDelay = 1000;

  function handleMessage(frame) {
    if (frame.type === "connected") {
      store.setConnectionStatus("replaying");
      return;
    }
    if (frame.type === "snapshot") {
      return;
    }
    if (frame.type === "replay.complete") {
      store.setConnectionStatus("live");
      return;
    }
    if (frame.type === "replay.event" || frame.type === "event") {
      store.applyEvent(frame.event);
      return;
    }
    if (frame.type === "thread.created" || frame.type === "thread.forked" || frame.type === "thread.updated") {
      store.applyThread(frame.thread);
      return;
    }
    if (frame.type === "turn.updated") {
      store.applyTurn(frame.turn);
      return;
    }
    if (frame.type === "approval.requested" || frame.type === "approval.responded") {
      store.applyApproval(frame.approval);
    }
  }

  function connect() {
    const state = store.getState();
    store.setConnectionStatus("connecting");
    const url = new URL("/ws", window.location.origin.replace("http", "ws"));
    url.searchParams.set("token", token);
    url.searchParams.set("lastEventId", String(state.lastEventId || 0));
    socket = new WebSocket(url.toString());

    socket.onmessage = (event) => {
      const frame = JSON.parse(event.data);
      handleMessage(frame);
      reconnectDelay = 1000;
    };

    socket.onclose = () => {
      store.setConnectionStatus("offline");
      window.setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 10000);
    };

    socket.onerror = () => {
      store.setConnectionStatus("error");
      socket.close();
    };
  }

  connect();

  return () => {
    if (socket) {
      socket.close();
    }
  };
}
