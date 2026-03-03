CODEx UI Wrapper — End-to-End Implementation Plan (Public-Ready MVP)
======================================================================
Date: 2026-03-02
Scope: A working, public-ready MVP you can build in a single coding session.
Assumption: You integrate via `codex app-server` over stdio JSONL (preferred),
            and expose your own stable HTTP+WebSocket API to a React UI.

0) Executive summary (what you will actually build)
---------------------------------------------------
You will build a local-only app with two components:

A) Backend (Python, FastAPI)
   - Spawns `codex app-server` as a child process (stdio pipes).
   - Speaks JSON-RPC (one JSON object per line) to Codex.
   - Persists threads/turns/events into SQLite.
   - Broadcasts streaming events to the frontend over *your* WebSocket.
   - Exposes REST endpoints for creating threads, running turns, forking, approvals.

B) Frontend (React, Vite)
   - Sidebar: thread list (with parent/child relationships).
   - Main: (1) graph of branches (turn-level nodes), (2) transcript view (event stream).
   - Composer: send prompt to selected thread.
   - Approval modal: approve/deny when Codex asks.

MVP features included:
- Create thread, resume thread list, run a turn with streaming output.
- Fork a thread at head (hard fork) and render the branching graph.
- Basic “import from branch” into another thread via preview/edit-confirmed copied context.

Public-ready properties:
- Binds to 127.0.0.1 by default.
- Uses a random session token for UI->backend requests.
- Never auto-approves; approvals require explicit user action.

1) Repo layout (scaffold)
------------------------
Create a repo like:

repo/
  backend/
    app/
      __init__.py
      main.py              # FastAPI entrypoint
      settings.py          # env/config
      db.py                # SQLite init + helpers
      models.py            # dataclasses/pydantic models
      codex_rpc.py         # JSON-RPC client over stdio
      codex_manager.py     # process lifecycle + per-thread sessions
      api.py               # routers
      ws.py                # WS hub/broadcast + replay
      security.py          # token middleware
      util.py
    codex_ui/
      __init__.py
      __main__.py          # python -m codex_ui dev
    tests/
      fake_app_server.py   # JSON-RPC stdio fixture for integration tests
    pyproject.toml
  frontend/
    index.html
    src/
      main.tsx
      api.ts
      ws.ts
      store.ts
      pages/
      components/
        ThreadList.tsx
        GraphView.tsx
        Transcript.tsx
        Composer.tsx
        ApprovalModal.tsx
        ImportPreviewModal.tsx
  README.md
  LICENSE
  SECURITY.md

2) Backend plan (FastAPI) — step-by-step
----------------------------------------

2.1 Settings and security (local-only, token-protected)
-------------------------------------------------------
Requirements:
- Listen on 127.0.0.1
- Generate a random token at startup (or load from file).
- Require the token in:
  - Authorization: Bearer <token> header for REST
  - Query param `?token=...` for WebSocket (simplest)

Pseudo-code: settings.py
------------------------
- Read env with defaults:
  - HOST=127.0.0.1
  - PORT=8787
  - DB_PATH=platform-appropriate app data dir (fallback to ~/.local/share/...)
  - CODEX_BIN=codex

Pseudo-code: security.py
------------------------
function generate_session_token():
    return secure_random_urlsafe(32)

middleware require_token(request):
    if request.path in ["/", "/static/*", "/health"]:
        allow
    token = request.header("Authorization").replace("Bearer ", "")
    if token != SETTINGS.SESSION_TOKEN:
        return 401

WS auth:
on websocket connect:
    token = ws.query_param("token")
    if token != SETTINGS.SESSION_TOKEN:
        close

Gotchas:
- Do not bind to 0.0.0.0 by default.
- Avoid CORS exposure; if you enable CORS, restrict to localhost only.

2.2 SQLite data model (event sourcing)
--------------------------------------
Use a simple schema. Store raw event payload JSON for forward compatibility.

Schema (SQLite):
- threads(thread_id TEXT PRIMARY KEY,
          title TEXT,
          created_at TEXT,
          updated_at TEXT,
          parent_thread_id TEXT NULL,
          forked_from_turn_id TEXT NULL,
          metadata_json TEXT)

- turns(turn_id TEXT PRIMARY KEY,
        thread_id TEXT,
        idx INTEGER,
        user_text TEXT,
        status TEXT,
        started_at TEXT,
        completed_at TEXT,
        metadata_json TEXT)

- events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,
         thread_id TEXT,
         turn_id TEXT,
         seq INTEGER,
         type TEXT,
         ts TEXT,
         payload_json TEXT)

Pseudo-code: db.py
------------------
function init_db():
    connect(DB_PATH)
    execute(CREATE TABLE IF NOT EXISTS ...)
    execute(CREATE INDEX IF NOT EXISTS idx_events_thread_turn_seq ON events(thread_id, turn_id, seq))
    return connection pool

function insert_thread(thread):
    upsert

function insert_turn(turn):
    upsert

function append_event(thread_id, turn_id, seq, type, payload):
    insert

function load_threads():
    select * from threads order by updated_at desc

function load_turns(thread_id):
    select * from turns where thread_id=? order by idx asc

function load_events(thread_id, turn_id):
    select * from events where thread_id=? and turn_id=? order by seq asc

Gotchas:
- Use WAL mode for concurrency.
- Batch writes (optional) if streaming is heavy.

2.3 Codex app-server JSON-RPC client (stdio JSONL)
--------------------------------------------------
Core requirement: robustly send/receive JSON lines and match responses by id.

Pseudo-code: codex_rpc.py
-------------------------
class JsonRpcError(Exception):
    code: int
    message: str
    data: any

class CodexRpcClient:
    def __init__(self, proc_stdin, proc_stdout, log_fn):
        self.stdin = proc_stdin
        self.stdout = proc_stdout
        self.log = log_fn
        self.next_id = 1
        self.pending = dict()  # id -> Future/Queue
        self.reader_task = start_background_task(self._reader_loop)

    async def _reader_loop(self):
        buffer = ""
        while True:
            line = await read_line_async(self.stdout)
            if line is None:
                # process exited
                fail_all_pending("Codex process ended")
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                self.log("WARN: invalid json line: " + line)
                continue

            if "id" in msg:
                # response
                fut = self.pending.pop(msg["id"], None)
                if fut:
                    fut.set_result(msg)
            else:
                # notification
                await self.on_notification(msg)

    async def request(self, method: str, params: dict, timeout_s=60):
        req_id = self.next_id
        self.next_id += 1
        msg = {"jsonrpc":"2.0","id":req_id,"method":method,"params":params}
        fut = make_future()
        self.pending[req_id] = fut
        await self._send(msg)

        resp = await wait_for(fut, timeout_s)
        if "error" in resp:
            err = resp["error"]
            raise JsonRpcError(err.get("code"), err.get("message"), err.get("data"))
        return resp["result"]

    async def notify(self, method: str, params: dict):
        msg = {"jsonrpc":"2.0","method":method,"params":params}
        await self._send(msg)

    async def _send(self, msg: dict):
        data = json.dumps(msg, separators=(",", ":")) + "\n"
        await write_async(self.stdin, data)
        await flush_async(self.stdin)

Backpressure gotcha:
- If you receive error code -32001, treat it as “overloaded, retry later”.
- Implement a wrapper function request_with_retry(method, params):
    backoff = [0.1, 0.2, 0.5, 1.0, 2.0]
    for delay in backoff:
        try: return await request(...)
        except JsonRpcError as e:
            if e.code == -32001:
                await sleep(delay)
                continue
            raise
    raise JsonRpcError(-32001, "Server overloaded; retries exhausted", None)

2.4 Process manager (one Codex process per active thread)
--------------------------------------------------------
For MVP simplicity and clean event separation:

- Each thread corresponds to a Codex app-server child process.
- Advantage: no interleaving events across threads.
- Tradeoff: more processes; acceptable for an MVP if you enforce limits and recovery.
- Session policy:
  - cap active child processes at 4 by default
  - evict least-recently-used idle sessions after 10 minutes
  - auto-resume by spawning a fresh child and calling `thread/resume`
  - retry one automatic restart on unexpected child exit, then surface a manual "Resume" action

Pseudo-code: codex_manager.py
-----------------------------
class CodexSession:
    thread_id: str
    proc: subprocess.Popen
    rpc: CodexRpcClient
    event_seq_by_turn: dict(turn_id -> int)
    active_turn_id: str | None

class CodexManager:
    def __init__(self, db, ws_hub):
        self.db = db
        self.ws = ws_hub
        self.sessions = dict() # thread_id -> CodexSession
        self.max_sessions = 4
        self.idle_ttl_s = 600

    async def start_session(self) -> CodexSession:
        proc = spawn(["codex", "app-server"], stdin=PIPE, stdout=PIPE, stderr=PIPE)
        rpc = CodexRpcClient(proc.stdin, proc.stdout, log_fn)
        session = CodexSession(proc=proc, rpc=rpc, ...)

        # handshake
        await rpc.request("initialize", {"client": {"name":"codex-ui-wrapper","version":"0.1.0"}})
        await rpc.notify("initialized", {})

        # create thread
        result = await rpc.request("thread/start", {})
        session.thread_id = result["threadId"] (or similar)
        self.sessions[session.thread_id] = session

        db.insert_thread(...)
        ws.broadcast({"type":"thread.created","thread":...})
        return session

    async def fork_thread(self, parent_thread_id: str) -> str:
        parent = self.sessions[parent_thread_id] or await self.attach_to_existing(parent_thread_id)
        # Hard fork (head)
        res = await parent.rpc.request("thread/fork", {"threadId": parent_thread_id})
        new_thread_id = res["threadId"]

        # Create a NEW session process for the forked thread (recommended):
        # Option A: if fork result is already in same server, you can continue in parent process.
        # Option B (clean): start new process and resume the forked threadId.
        child = await self.resume_in_new_process(new_thread_id)

        db.insert_thread(thread_id=new_thread_id, parent_thread_id=parent_thread_id, forked_from_turn_id=last_turn_id)
        ws.broadcast({"type":"thread.forked", ...})
        return new_thread_id

    async def resume_in_new_process(self, thread_id: str) -> CodexSession:
        proc = spawn(["codex","app-server"], ...)
        rpc = CodexRpcClient(...)
        await rpc.request("initialize", ...)
        await rpc.notify("initialized", {})
        await rpc.request("thread/resume", {"threadId": thread_id})
        # store session and return

Gotchas:
- The exact response field names depend on schema/version. Use schema generation or defensive parsing.
- Thread IDs might be long; treat as opaque.
- Ensure you kill processes on app exit.
- Enforce the session cap before spawning a new child; resume threads on demand instead of keeping every thread hot forever.

2.5 Notification handling → DB + WebSocket
------------------------------------------
Your CodexRpcClient should call a handler for notifications.

Pseudo-code: in CodexRpcClient, set:
    self.on_notification = manager.handle_notification(session, msg)

Pseudo-code: codex_manager.py
-----------------------------
async def handle_notification(self, session: CodexSession, msg: dict):
    # Determine current thread_id and turn_id from msg
    # Codex protocol typically includes them in params; if not, infer from session state.
    thread_id = session.thread_id
    turn_id = extract_turn_id(msg) or session.active_turn_id

    # Sequence counter per turn
    seq = session.event_seq_by_turn.get(turn_id, 0) + 1
    session.event_seq_by_turn[turn_id] = seq

    # Persist and broadcast
    event_id = db.append_event(thread_id, turn_id, seq, msg.get("method","notification"), msg.get("params"))
    ws.broadcast({
        "type":"event",
        "event":{
            "eventId": event_id,
            "threadId":thread_id,
            "turnId":turn_id,
            "seq":seq,
            "type":msg.get("method","notification"),
            "ts":now(),
            "payload":msg.get("params"),
        }
    })

Gotchas:
- Some notifications may arrive before you set active_turn_id; store with turn_id=None or a special placeholder.
- You MUST also handle stderr logs for debugging and optionally show them in UI.
- Assign a global monotonically increasing `event_id` so WebSocket reconnect can replay from `lastEventId`.

2.6 Turn lifecycle: start, stream, complete
-------------------------------------------
Pseudo-code: start turn endpoint handler
---------------------------------------
POST /api/threads/{thread_id}/turns  body: {text: "..."}

async def start_turn(thread_id, text):
    session = manager.get_or_resume(thread_id)
    if session.active_turn_id is not None:
        raise 409 "Turn already running"

    try:
        res = await session.rpc.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type":"text","text":text}]
        }, timeout_s=600)

        # Canonical rule: use the server-returned turnId or fail fast.
        turn_id = res.get("turnId")
        if not turn_id:
            raise UnsupportedCodexVersion("turn/start must return turnId")

        db.insert_turn(turn_id, thread_id, idx=next_idx, user_text=text, status="running")
        ws.broadcast({"type":"turn.started", "turnId":turn_id, ...})
        session.active_turn_id = turn_id

        # completion is usually a notification; but also set status here if appropriate
        db.update_turn_status(turn_id, "submitted")
        return {"turnId": turn_id}
    except JsonRpcError as e:
        if session.active_turn_id:
            db.update_turn_status(session.active_turn_id, "error")
        ws.broadcast({"type":"turn.error","error":...})
        session.active_turn_id = None
        raise
    finally:
        # do not clear active_turn_id here if completion is async; clear when you see turn/completed notification.
        pass

Notification-based completion:
- When you see a "turn/completed" (or similar) notification:
    db.update_turn_status(turn_id, "completed", completed_at=now)
    session.active_turn_id = None
    ws.broadcast({"type":"turn.completed", ...})

Gotchas:
- Treat the Codex-returned `turnId` as canonical; do not create provisional local IDs or migrations.
- Notification names can still drift, but the supported release should pin a Codex CLI version and verify required methods at startup.

2.7 Approvals
-------------
Codex may emit an “approval requested” notification. Your UI must:
- show details
- allow approve or deny
- send an approval response method back

Pseudo-code (conceptual):
-------------------------
# When notification indicates approval required:
approval_id = msg.params.approvalId
store approval record in DB (optional table or as event)
ws.broadcast({"type":"approval.requested","approvalId":..., "details":...})

# REST endpoint:
POST /api/approvals/{approval_id} body: {decision:"approve"|"deny"}

async def respond_approval(approval_id, decision):
    session = find_session_for_approval(approval_id)
    await session.rpc.request("approval/respond", {"approvalId": approval_id, "decision": decision})
    ws.broadcast({"type":"approval.responded", ...})

Gotchas:
- For the supported public release, method names should be hard-required and startup should fail fast if the installed Codex CLI does not match the expected contract.
- NEVER auto-approve. Public-ready means explicit user action.

2.8 Schema drift defense (public-ready)
---------------------------------------
At backend startup:
- Require Codex CLI version `0.23.x`.
- Run `codex app-server generate-json-schema --out <cache_dir>`
- Load schema; verify required methods and notifications exist.
- If not, show a clear error page: “Unsupported Codex version; please upgrade/downgrade.”

- Hard-require these methods: `initialize`, `thread/start`, `thread/resume`, `thread/fork`, `thread/list`, `thread/read`, `turn/start`, `approval/respond`
- Hard-require these notifications: `initialized`, `turn/started`, `turn/completed`, `turn/failed`, `item/started`, `item/completed`, `item/agentMessage/delta`

Pseudo-code:
------------
function ensure_schema():
    assert codex --version matches 0.23.*
    run subprocess: ["codex","app-server","generate-json-schema","--out",cache_dir]
    if returncode != 0:
        warn but continue best-effort
    else:
        schema = load(cache_dir)
        assert methods needed in schema
        assert required notifications needed in schema

Gotchas:
- On Windows, subprocess quoting and path.
- Cache directory permissions.

3) Frontend plan (React) — step-by-step
---------------------------------------

3.1 API client (REST) and event stream (WS)
-------------------------------------------
src/api.ts
----------
async function apiGet(path):
    return fetch(path, headers={"Authorization":"Bearer "+TOKEN})

async function apiPost(path, body):
    return fetch(path, method="POST", json body, auth header)

src/ws.ts
---------
function connectWs():
    ws = new WebSocket(`ws://127.0.0.1:8787/ws?token=${TOKEN}&lastEventId=${STORE.lastEventId}`)
    ws.onmessage = (ev) => dispatch(JSON.parse(ev.data))
    # Expect frames in this order:
    # 1) connected
    # 2) snapshot
    # 3) zero or more replay.event
    # 4) replay.complete
    # 5) live event / approval / thread convenience frames

Gotchas:
- Reconnect logic with exponential backoff.
- If token invalid, show “Restart backend” guidance.
- Reconnect must use the latest stored `lastEventId` cursor so no persisted events are lost or duplicated.

3.2 State management
--------------------
Keep state minimal:
- threads: map threadId -> thread
- turns: map threadId -> array turnIds
- events: map (threadId, turnId) -> event list
- ui selection: selectedThreadId, selectedTurnId
- approvals: pending approvals list
- lastEventId: global replay cursor from persisted event stream
- connection phase: disconnected | replaying | live

3.3 Thread list component
-------------------------
Show parent/child hierarchy, allow selection.

Pseudo-code:
------------
renderThread(thread):
    indent = depth(parent chain)
    show title
    actions: select, fork, new turn

3.4 Graph view (turn-level DAG)
-------------------------------
Use React Flow or a minimal SVG layout.

Layout strategy for MVP:
- Each thread gets a “lane” (vertical band).
- X axis: turn index.
- Node position:
    x = turn.idx * X_SPACING
    y = laneIndex(threadId) * Y_SPACING
- Edge:
    - between consecutive turns in same thread
    - from parent thread fork point to first turn of child thread

Pseudo-code:
------------
buildGraph(threads, turns):
    lanes = topo-sort threads by parent->child order
    nodes = []
    edges = []
    for each thread:
        for each turn in thread:
            nodes.push({id: turnId, position:{x,y}, data:{...}})
            if prevTurn: edges.push(prevTurn -> turn)
        if thread.parentThreadId:
            forkTurn = thread.forkedFromTurnId
            firstTurn = first turn in child
            edges.push(forkTurn -> firstTurn)

Gotchas:
- A child thread may have 0 turns (brand new). Create a “thread head” node.
- Keep the graph readable: only turns, not individual events.

3.5 Transcript view (streamed events)
-------------------------------------
Render:
- user message
- agent deltas combined into paragraphs
- tool actions and results
- approvals

Optimization gotcha:
- Delta events can be many. Buffer them:
    - append deltas to a string
    - flush to UI every 50–100ms

Pseudo-code:
------------
onEvent(event):
    if event.type == "item/agentMessage/delta":
        buffer += event.payload.delta
        if timeSinceLastFlush > 75ms: setState(bufferedText)
    else:
        flush buffer
        render non-delta event as a block

3.6 Composer + turn submit
--------------------------
Disable send if:
- no selected thread
- backend indicates thread has active turn running

On submit:
- POST /api/threads/{id}/turns
- UI optimistically adds turn; then listens for events

4) “Import from branch” (soft merge) MVP
----------------------------------------
Goal: move info from one branch to another without pretending it’s a true merge.

UI flow:
- Select source thread, choose turns range
- Click “Import to …”
- Backend assembles a transfer blob preview:
    - headings: source thread title + turn ids
    - include final agent output from each turn (deterministic extraction)
    - include list of commands run + success/failure where available
- Backend runs best-effort secret detection and highlights suspected secrets
- User confirms or edits the previewed blob
- Backend POSTs a new turn into destination thread with the confirmed blob

Pseudo-code: backend import
--------------------------
POST /api/import/preview  {sourceThreadId, sourceTurnIds[], destThreadId}

def build_transfer_blob(sourceThreadId, sourceTurnIds):
    events = load events for those turns
    extract:
        - user prompts
        - final agent message text (or concatenated deltas)
        - tool command summaries
    return formatted text

async def preview_import(...):
    blob = build_transfer_blob(...)
    suspected_secrets = detect_secrets(blob)
    return {"previewId": ..., "transferBlob": blob, "suspectedSecrets": suspected_secrets}

POST /api/import/commit  {previewId, confirmed, editedTransferBlob}

async def import_to_dest(...):
    require confirmed == true
    await start_turn(destThreadId, editedTransferBlob)

Gotchas:
- You must not include secrets in the blob. Consider redaction rules:
  - require preview/edit confirmation even after best-effort detection.
- Make it explicit in UI: “This is copied context, not a true merge.”

5) Local log indexing (optional, but helpful)
---------------------------------------------
Codex stores logs in a sessions directory.
If you add indexing:
- Scan ~.codex/sessions and parse JSONL.
- Populate your SQLite threads/turns/events for offline browsing.

MVP recommendation:
- Skip indexing in the first session; rely on DB you capture live.
- Add indexing after you confirm streaming works.

6) Testing plan (must-have smoke tests)
---------------------------------------
Backend unit tests:
- JSON-RPC framing (request/response matching)
- Overload retry for -32001
- DB migration/init
- Replay cursor logic (`lastEventId`, snapshot, replay ordering)

Integration tests (required for public MVP):
- Run a fake `codex app-server` over stdio JSON-RPC
- Verify create thread, turn start, streaming deltas, completion, overload retry, approval round-trip, reconnect replay, and child crash recovery

Frontend tests (optional):
- Minimal: ensure WS reconnect and event rendering do not crash.

7) Deployment and release checklist
-----------------------------------
- Provide a cross-platform Python CLI:
    python -m codex_ui dev  # starts backend, optional frontend dev server, opens browser
- Bind to 127.0.0.1 only.
- Generate token at first run; print it and store locally.
- Add SECURITY.md and a responsible disclosure email.
- Add license, notices, and clear privacy statement.
- Refuse startup if Codex CLI version is outside the supported `0.23.x` range.

8) Step-by-step “single coding session” execution order
-------------------------------------------------------
Order matters—this is the fastest path to a working MVP:

(1) Backend skeleton (FastAPI, /health, token middleware, version check)
(2) SQLite init + simple CRUD for threads/turns/events + global event replay cursor
(3) Codex process spawn + JSON-RPC send/receive + initialize handshake
(4) Implement: thread/start + turn/start using server-returned `turnId`
(5) Implement notification persistence + WS snapshot/replay/live stream
(6) Frontend: connect WS with `lastEventId`, render transcript deltas
(7) Add thread list + selection
(8) Add fork at head (thread/fork) + DB parent linkage + session resume rules
(9) Add approvals modal and explicit approval methods
(10) Add import preview/edit-confirm flow
(11) Add fake app-server integration suite
(12) Package: bundle frontend build into backend

9) Minimal pseudo-code “glue” (backend main)
--------------------------------------------
app/main.py
-----------
def create_app():
    settings = load_settings()
    token = load_or_create_token()
    ensure_supported_codex_version()
    ensure_schema()
    db = init_db()
    ws_hub = WebSocketHub()
    codex_manager = CodexManager(db, ws_hub, settings)

    app = FastAPI()

    app.add_middleware(TokenMiddleware, token=token)

    @app.get("/health")
    def health(): return {"ok": True}

    # REST routes
    app.include_router(api_router(db, codex_manager))

    # WS route
    @app.websocket("/ws")
    async def ws_endpoint(websocket):
        await ws_hub.accept(websocket, token_check=True)
        last_event_id = websocket.query_params.get("lastEventId")
        await ws_hub.send_initial_snapshot(websocket, db, last_event_id=last_event_id)
        await ws_hub.run_forever(websocket)

    return app

10) Notes on “gotchas” you should document publicly
---------------------------------------------------
- Forking fidelity: depending on Codex version, “fork from arbitrary turn” may be a soft fork.
- Large streaming outputs: UI batches deltas.
- Local-only and approval-based operations: never auto-approve.
- Version drift: wrapper attempts to defend via schema generation; if unsupported, instruct user to upgrade/downgrade.
- Reconnect behavior depends on persisted `event_id` replay; clients should keep the latest `lastEventId`.
- Import is copied context with an explicit preview/edit gate, not a true merge.
- One-process-per-thread is bounded by session caps, idle eviction, and on-demand resume.




Appendix A) Targeted fixes adopted from review
==============================================
Date: 2026-03-02

1) Protocol contract ambiguity (RPC / turn / approvals)
------------------------------------------------------
Pin to Codex CLI version: **v0.23.0** (wrapper refuses to run if `codex --version` != 0.23.*), and hard-require these exact JSON-RPC methods: `initialize`, `initialized` (notification), `thread/start`, `thread/resume`, `thread/fork`, `thread/list`, `thread/read`, `turn/start`, `approval/respond`, plus notifications `turn/started`, `turn/completed`, `turn/failed`, `item/started`, `item/completed`, and `item/agentMessage/delta` (anything else is stored as an opaque event but not relied upon).

2) Turn-ID strategy fragility
-----------------------------
Adopt a single canonical rule: **the turn ID is whatever `turn/start` returns as `turnId` (hard requirement; if absent, abort the turn and show “unsupported Codex version”),** and all events are keyed by `(threadId, turnId)` with no provisional IDs or migrations.

3) Notification replay semantics (reconnect cursor/snapshot)
------------------------------------------------------------
Implement explicit replay with a monotonic cursor: backend assigns every stored event a global `event_id` (SQLite AUTOINCREMENT) and the frontend reconnects with `lastEventId`, after which backend sends (a) a compact snapshot of thread/turn headers and (b) all events with `event_id > lastEventId` in order, then switches to live streaming.

4) “Public-ready” testing (fake app-server integration)
-------------------------------------------------------
Add an automated integration suite that runs a **fake codex app-server** (a small Python JSON-RPC stdio server that simulates required methods, streaming deltas, overload `-32001`, approvals, and crash/restart) and verifies the backend’s request/retry logic, persistence, cursor replay correctness, and approval round-trips end-to-end.

5) Import/redaction under-specified
-----------------------------------
Replace “regex-only redaction” with a **preview/edit gate**: backend assembles the transfer blob + runs best-effort detection (regex + entropy heuristics + known-token format checks), then UI shows a diffable preview with highlighted suspected secrets and requires the user to confirm or edit before the blob is submitted as a new turn.

6) Cross-platform scaffolding (scripts/dev.sh)
----------------------------------------------
Remove `scripts/dev.sh` and replace it with a single cross-platform Python CLI (`python -m codex_ui dev`) that starts backend, starts frontend dev server (optional), and opens the browser, so macOS/Linux/Windows use the same entrypoint.

7) One-process-per-thread scaling (limits, eviction, crash recovery)
--------------------------------------------------------------------
Define explicit session limits and policies: cap at N active processes (default 4), evict least-recently-used idle sessions after T minutes (default 10) by cleanly shutting down the child, auto-resume by spawning a new child on demand, and implement crash recovery by marking the session “dead” on exit, retrying a single automatic restart, and otherwise surfacing a “Resume” button that reattaches via `thread/resume`.

11) What needs changing
-----------------------
- No remaining consistency items from this pass. Future edits should continue moving completed bullets out of this section instead of duplicating them elsewhere.

12) What has changed
--------------------
- Added "What needs changing" and "What has changed" tracking sections so future edits can move bullets between them instead of losing scope history.
- Added a public REST and WebSocket API appendix that only covers the local-only wrapper behaviors described in this plan.
- Added a verbose reference backend and frontend code sketch so implementers have a concrete shape for models, routes, events, approvals, and reconnection.
- Kept the public scope narrow: localhost-only, token-protected, approval-gated, thread/turn/fork/import focused, with no extra product features outside the stated MVP.
- Folded the stricter appendix contract back into the earlier backend, frontend, testing, and release sections so the plan now reads consistently from top to bottom.
- Replaced the old `scripts/dev.sh` scaffold reference with a cross-platform Python CLI entrypoint in both the repo layout and release checklist.
- Added replay-cursor language (`lastEventId`, snapshot, replay, live stream) to the WebSocket, state-management, testing, and glue sections.
- Promoted the fake `codex app-server` integration suite into the main testing plan as required coverage for the public MVP.
- Promoted the import preview/edit gate into the main import flow so copied-context behavior is explicit in both backend and UI steps.
- Added explicit session-cap, idle-eviction, and resume/restart policy language to the process-manager section.

13) Public API description
--------------------------
This wrapper has exactly one public purpose: expose a stable, local-only HTTP + WebSocket interface for creating Codex-backed threads, running turns, forking, replaying event history, responding to approvals, and importing copied context between branches. It does **not** attempt to expose arbitrary Codex internals, remote multi-user access, cloud sync, plugin execution, or any workflow not already described in this plan.

Authentication and transport
----------------------------
- Bind host: `127.0.0.1`
- Default port: `8787`
- REST auth: `Authorization: Bearer <session-token>`
- WebSocket auth: `ws://127.0.0.1:8787/ws?token=<session-token>&lastEventId=<optional-int>`
- Content type: `application/json`
- Time format: UTC ISO-8601 strings
- Thread IDs and turn IDs are opaque strings returned by Codex and must never be parsed by clients
- Event ordering:
  - `event_id` is the global replay cursor
  - `seq` is the per-turn display order

Common REST error model
-----------------------
Every non-2xx response uses:

```json
{
  "error": {
    "code": "string_machine_code",
    "message": "Human readable description",
    "details": {}
  }
}
```

Common error codes:
- `unauthorized`: missing or invalid bearer token
- `unsupported_codex_version`: installed Codex CLI does not match the supported contract
- `thread_not_found`: the requested thread ID is unknown to the wrapper
- `turn_in_progress`: the target thread already has an active turn
- `approval_not_found`: the approval ID is unknown or no longer pending
- `invalid_request`: request body is malformed or fails validation
- `codex_rpc_error`: Codex returned a protocol-level error
- `codex_process_unavailable`: Codex child process exited or failed to start
- `import_preview_required`: import submission attempted without preview confirmation

REST endpoints
--------------

`GET /health`
- Purpose: liveness check for the local service
- Auth: no auth required
- Response:

```json
{
  "ok": true,
  "service": "codex-ui-wrapper",
  "version": "0.1.0"
}
```

`GET /api/bootstrap`
- Purpose: load the minimum snapshot needed for first render or after a full page refresh
- Auth: bearer token required
- Query params:
  - `afterEventId` (optional integer): if present, only return events newer than this cursor
- Response:

```json
{
  "serverTime": "2026-03-02T23:00:00Z",
  "snapshot": {
    "threads": [
      {
        "threadId": "thr_123",
        "title": "Example thread",
        "createdAt": "2026-03-02T22:00:00Z",
        "updatedAt": "2026-03-02T22:01:00Z",
        "parentThreadId": null,
        "forkedFromTurnId": null,
        "status": "idle",
        "metadata": {}
      }
    ],
    "turns": [
      {
        "turnId": "turn_001",
        "threadId": "thr_123",
        "idx": 1,
        "userText": "Say hello",
        "status": "completed",
        "startedAt": "2026-03-02T22:00:10Z",
        "completedAt": "2026-03-02T22:00:12Z",
        "metadata": {}
      }
    ],
    "pendingApprovals": []
  },
  "events": [
    {
      "eventId": 1,
      "threadId": "thr_123",
      "turnId": "turn_001",
      "seq": 1,
      "type": "turn.started",
      "ts": "2026-03-02T22:00:10Z",
      "payload": {}
    }
  ],
  "lastEventId": 1
}
```

`GET /api/threads`
- Purpose: list all known threads in reverse update order
- Auth: bearer token required
- Response:

```json
{
  "threads": [
    {
      "threadId": "thr_123",
      "title": "Example thread",
      "createdAt": "2026-03-02T22:00:00Z",
      "updatedAt": "2026-03-02T22:01:00Z",
      "parentThreadId": null,
      "forkedFromTurnId": null,
      "status": "idle",
      "metadata": {}
    }
  ]
}
```

`POST /api/threads`
- Purpose: start a new Codex thread and persist it
- Auth: bearer token required
- Request body:

```json
{
  "title": "Optional UI title"
}
```

- Response:

```json
{
  "thread": {
    "threadId": "thr_456",
    "title": "Optional UI title",
    "createdAt": "2026-03-02T23:01:00Z",
    "updatedAt": "2026-03-02T23:01:00Z",
    "parentThreadId": null,
    "forkedFromTurnId": null,
    "status": "idle",
    "metadata": {}
  }
}
```

`GET /api/threads/{threadId}`
- Purpose: fetch one thread plus its turns
- Auth: bearer token required
- Response:

```json
{
  "thread": {
    "threadId": "thr_123",
    "title": "Example thread",
    "createdAt": "2026-03-02T22:00:00Z",
    "updatedAt": "2026-03-02T22:01:00Z",
    "parentThreadId": null,
    "forkedFromTurnId": null,
    "status": "idle",
    "metadata": {}
  },
  "turns": [
    {
      "turnId": "turn_001",
      "threadId": "thr_123",
      "idx": 1,
      "userText": "Say hello",
      "status": "completed",
      "startedAt": "2026-03-02T22:00:10Z",
      "completedAt": "2026-03-02T22:00:12Z",
      "metadata": {}
    }
  ]
}
```

`GET /api/threads/{threadId}/events`
- Purpose: fetch persisted events for one thread, optionally after a replay cursor
- Auth: bearer token required
- Query params:
  - `afterEventId` (optional integer)
  - `limit` (optional integer, default `500`, max `5000`)
- Response:

```json
{
  "events": [
    {
      "eventId": 10,
      "threadId": "thr_123",
      "turnId": "turn_001",
      "seq": 4,
      "type": "item/agentMessage/delta",
      "ts": "2026-03-02T22:00:11Z",
      "payload": {
        "delta": "Hello"
      }
    }
  ],
  "lastEventId": 10
}
```

`POST /api/threads/{threadId}/turns`
- Purpose: submit a new prompt into an existing thread
- Auth: bearer token required
- Request body:

```json
{
  "text": "Say hello",
  "clientRequestId": "optional-idempotency-key"
}
```

- Response:

```json
{
  "turn": {
    "turnId": "turn_002",
    "threadId": "thr_123",
    "idx": 2,
    "userText": "Say hello",
    "status": "running",
    "startedAt": "2026-03-02T23:05:00Z",
    "completedAt": null,
    "metadata": {}
  }
}
```

`POST /api/threads/{threadId}/fork`
- Purpose: hard-fork a thread at head and create a child thread record
- Auth: bearer token required
- Request body:

```json
{
  "title": "Optional fork title"
}
```

- Response:

```json
{
  "thread": {
    "threadId": "thr_child",
    "title": "Optional fork title",
    "createdAt": "2026-03-02T23:10:00Z",
    "updatedAt": "2026-03-02T23:10:00Z",
    "parentThreadId": "thr_123",
    "forkedFromTurnId": "turn_002",
    "status": "idle",
    "metadata": {}
  }
}
```

`POST /api/approvals/{approvalId}`
- Purpose: explicitly approve or deny a pending approval request
- Auth: bearer token required
- Request body:

```json
{
  "decision": "approve"
}
```

- Response:

```json
{
  "approvalId": "apr_001",
  "decision": "approve",
  "status": "submitted"
}
```

`POST /api/import/preview`
- Purpose: build a copied-context preview from selected source turns before the user confirms submission
- Auth: bearer token required
- Request body:

```json
{
  "sourceThreadId": "thr_source",
  "sourceTurnIds": ["turn_010", "turn_011"],
  "destThreadId": "thr_dest"
}
```

- Response:

```json
{
  "previewId": "imp_prev_001",
  "destThreadId": "thr_dest",
  "sourceThreadId": "thr_source",
  "sourceTurnIds": ["turn_010", "turn_011"],
  "suspectedSecrets": [
    {
      "label": "Possible API key",
      "start": 120,
      "end": 152
    }
  ],
  "transferBlob": "Imported context:\n\nSource thread: ...",
  "expiresAt": "2026-03-02T23:20:00Z"
}
```

`POST /api/import/commit`
- Purpose: submit a previously previewed and optionally user-edited transfer blob into the destination thread
- Auth: bearer token required
- Request body:

```json
{
  "previewId": "imp_prev_001",
  "confirmed": true,
  "editedTransferBlob": "Imported context:\n\n..."
}
```

- Response:

```json
{
  "importedIntoTurnId": "turn_099",
  "destThreadId": "thr_dest",
  "status": "running"
}
```

WebSocket protocol
------------------
The WebSocket is the live event stream for the wrapper. The backend sends an initial connected frame, then a snapshot/replay response, then live events. The client does not send arbitrary commands on the socket; all state-changing operations remain HTTP POSTs so auth, validation, and retries stay simple.

Server -> client frames:

`connected`

```json
{
  "type": "connected",
  "serverTime": "2026-03-02T23:00:00Z",
  "replayFromEventId": 100
}
```

`snapshot`

```json
{
  "type": "snapshot",
  "snapshot": {
    "threads": [],
    "turns": [],
    "pendingApprovals": []
  }
}
```

`replay.event`

```json
{
  "type": "replay.event",
  "event": {
    "eventId": 101,
    "threadId": "thr_123",
    "turnId": "turn_002",
    "seq": 1,
    "type": "turn.started",
    "ts": "2026-03-02T23:05:00Z",
    "payload": {}
  }
}
```

`replay.complete`

```json
{
  "type": "replay.complete",
  "lastEventId": 110
}
```

`event`

```json
{
  "type": "event",
  "event": {
    "eventId": 111,
    "threadId": "thr_123",
    "turnId": "turn_002",
    "seq": 2,
    "type": "item/agentMessage/delta",
    "ts": "2026-03-02T23:05:01Z",
    "payload": {
      "delta": "Hello"
    }
  }
}
```

`approval.requested`

```json
{
  "type": "approval.requested",
  "approval": {
    "approvalId": "apr_001",
    "threadId": "thr_123",
    "turnId": "turn_002",
    "kind": "shell",
    "details": {
      "command": "git status"
    }
  }
}
```

`thread.created`, `thread.forked`, `turn.updated`, and `approval.responded`
- These are convenience frames emitted in addition to raw stored events so the UI can update sidebars and modal state without reparsing transcript payloads.
- Each frame must include enough data to update local state without requiring an immediate follow-up GET.

14) Reference implementation sketch
-----------------------------------
The following code is intentionally verbose. It is not meant to be a finished drop-in implementation, but it is concrete enough that an implementer can translate it almost line-for-line into the real backend and frontend while keeping the public scope limited to the behavior described above.

```python
# backend/app/reference_contract_example.py
#
# Public-scope reference only:
# - local FastAPI wrapper
# - bearer-token REST
# - tokenized WebSocket replay/live stream
# - thread / turn / fork / approval / import-preview / import-commit
# - no remote auth, no multi-user tenancy, no extra product surface

from __future__ import annotations

import asyncio
import json
import secrets
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket
from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


SESSION_TOKEN = secrets.token_urlsafe(32)


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            status_code=status_code,
            detail={"error": {"code": code, "message": message, "details": details or {}}},
        )


def require_token(authorization: str | None = Header(default=None)) -> str:
    expected = f"Bearer {SESSION_TOKEN}"
    if authorization != expected:
        raise ApiError(401, "unauthorized", "Missing or invalid bearer token")
    return SESSION_TOKEN


class ThreadRecord(BaseModel):
    threadId: str
    title: str | None = None
    createdAt: str
    updatedAt: str
    parentThreadId: str | None = None
    forkedFromTurnId: str | None = None
    status: str = "idle"
    metadata: dict[str, Any] = Field(default_factory=dict)


class TurnRecord(BaseModel):
    turnId: str
    threadId: str
    idx: int
    userText: str
    status: str
    startedAt: str
    completedAt: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EventRecord(BaseModel):
    eventId: int
    threadId: str
    turnId: str | None = None
    seq: int
    type: str
    ts: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalRecord(BaseModel):
    approvalId: str
    threadId: str
    turnId: str
    kind: str
    details: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"


class CreateThreadRequest(BaseModel):
    title: str | None = None


class StartTurnRequest(BaseModel):
    text: str = Field(min_length=1)
    clientRequestId: str | None = None


class ForkThreadRequest(BaseModel):
    title: str | None = None


class ApprovalDecisionRequest(BaseModel):
    decision: str


class ImportPreviewRequest(BaseModel):
    sourceThreadId: str
    sourceTurnIds: list[str]
    destThreadId: str


class ImportCommitRequest(BaseModel):
    previewId: str
    confirmed: bool
    editedTransferBlob: str


@dataclass
class InMemoryDb:
    threads: dict[str, ThreadRecord] = field(default_factory=dict)
    turns_by_thread: dict[str, list[TurnRecord]] = field(default_factory=lambda: defaultdict(list))
    events: list[EventRecord] = field(default_factory=list)
    approvals: dict[str, ApprovalRecord] = field(default_factory=dict)
    previews: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_event_id: int = 1

    def list_threads(self) -> list[ThreadRecord]:
        return sorted(self.threads.values(), key=lambda item: item.updatedAt, reverse=True)

    def get_thread(self, thread_id: str) -> ThreadRecord:
        thread = self.threads.get(thread_id)
        if not thread:
            raise ApiError(404, "thread_not_found", f"Unknown thread: {thread_id}")
        return thread

    def get_turns(self, thread_id: str) -> list[TurnRecord]:
        self.get_thread(thread_id)
        return list(self.turns_by_thread[thread_id])

    def append_event(self, thread_id: str, turn_id: str | None, event_type: str, payload: dict[str, Any]) -> EventRecord:
        current_turn_events = [event for event in self.events if event.threadId == thread_id and event.turnId == turn_id]
        event = EventRecord(
            eventId=self.next_event_id,
            threadId=thread_id,
            turnId=turn_id,
            seq=len(current_turn_events) + 1,
            type=event_type,
            ts=utc_now(),
            payload=payload,
        )
        self.events.append(event)
        self.next_event_id += 1
        return event


@dataclass
class WebSocketHub:
    sockets: set[WebSocket] = field(default_factory=set)

    async def connect(self, websocket: WebSocket, token: str) -> None:
        if token != SESSION_TOKEN:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        self.sockets.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        self.sockets.discard(websocket)

    async def send_json(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(payload))

    async def broadcast(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for websocket in self.sockets:
            try:
                await websocket.send_text(json.dumps(payload))
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self.sockets.discard(websocket)


@dataclass
class FakeCodexManager:
    db: InMemoryDb
    ws: WebSocketHub

    async def create_thread(self, title: str | None) -> ThreadRecord:
        thread_id = f"thr_{len(self.db.threads) + 1:03d}"
        thread = ThreadRecord(
            threadId=thread_id,
            title=title or "Untitled thread",
            createdAt=utc_now(),
            updatedAt=utc_now(),
        )
        self.db.threads[thread_id] = thread
        await self.ws.broadcast({"type": "thread.created", "thread": thread.model_dump()})
        return thread

    async def fork_thread(self, parent_thread_id: str, title: str | None) -> ThreadRecord:
        parent = self.db.get_thread(parent_thread_id)
        parent_turns = self.db.get_turns(parent_thread_id)
        child = ThreadRecord(
            threadId=f"thr_{len(self.db.threads) + 1:03d}",
            title=title or f"Fork of {parent.title}",
            createdAt=utc_now(),
            updatedAt=utc_now(),
            parentThreadId=parent.threadId,
            forkedFromTurnId=parent_turns[-1].turnId if parent_turns else None,
        )
        self.db.threads[child.threadId] = child
        await self.ws.broadcast({"type": "thread.forked", "thread": child.model_dump()})
        return child

    async def start_turn(self, thread_id: str, text: str) -> TurnRecord:
        thread = self.db.get_thread(thread_id)
        turns = self.db.turns_by_thread[thread_id]
        if any(turn.status == "running" for turn in turns):
            raise ApiError(409, "turn_in_progress", f"Thread {thread_id} already has a running turn")

        turn = TurnRecord(
            turnId=f"turn_{len(turns) + 1:03d}",
            threadId=thread_id,
            idx=len(turns) + 1,
            userText=text,
            status="running",
            startedAt=utc_now(),
        )
        turns.append(turn)
        thread.updatedAt = utc_now()

        started = self.db.append_event(thread_id, turn.turnId, "turn.started", {"userText": text})
        await self.ws.broadcast({"type": "event", "event": started.model_dump()})

        for chunk in ["Hello", ", ", "world", "."]:
            await asyncio.sleep(0.01)
            delta = self.db.append_event(thread_id, turn.turnId, "item/agentMessage/delta", {"delta": chunk})
            await self.ws.broadcast({"type": "event", "event": delta.model_dump()})

        turn.status = "completed"
        turn.completedAt = utc_now()
        completed = self.db.append_event(thread_id, turn.turnId, "turn.completed", {})
        await self.ws.broadcast({"type": "turn.updated", "turn": turn.model_dump()})
        await self.ws.broadcast({"type": "event", "event": completed.model_dump()})
        return turn

    async def create_preview(self, request: ImportPreviewRequest) -> dict[str, Any]:
        self.db.get_thread(request.sourceThreadId)
        self.db.get_thread(request.destThreadId)
        source_turns = [turn for turn in self.db.get_turns(request.sourceThreadId) if turn.turnId in request.sourceTurnIds]
        transfer_blob = "Imported context:\n\n" + "\n\n".join(
            f"Turn {turn.turnId}\nUser: {turn.userText}\nAssistant summary: (extract final assistant text here)"
            for turn in source_turns
        )
        preview_id = f"imp_prev_{len(self.db.previews) + 1:03d}"
        preview = {
            "previewId": preview_id,
            "destThreadId": request.destThreadId,
            "sourceThreadId": request.sourceThreadId,
            "sourceTurnIds": request.sourceTurnIds,
            "suspectedSecrets": [],
            "transferBlob": transfer_blob,
            "expiresAt": utc_now(),
        }
        self.db.previews[preview_id] = preview
        return preview

    async def commit_preview(self, request: ImportCommitRequest) -> dict[str, Any]:
        preview = self.db.previews.get(request.previewId)
        if not preview:
            raise ApiError(404, "invalid_request", "Unknown import preview")
        if not request.confirmed:
            raise ApiError(400, "import_preview_required", "Import must be explicitly confirmed")
        turn = await self.start_turn(preview["destThreadId"], request.editedTransferBlob)
        return {"importedIntoTurnId": turn.turnId, "destThreadId": turn.threadId, "status": turn.status}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = InMemoryDb()
    ws = WebSocketHub()
    manager = FakeCodexManager(db=db, ws=ws)
    app.state.db = db
    app.state.ws = ws
    app.state.manager = manager
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "codex-ui-wrapper", "version": "0.1.0"}


@app.get("/api/bootstrap")
async def bootstrap(
    afterEventId: int | None = Query(default=None),
    _: str = Depends(require_token),
) -> dict[str, Any]:
    db: InMemoryDb = app.state.db
    events = [event for event in db.events if afterEventId is None or event.eventId > afterEventId]
    turns = [turn for turns in db.turns_by_thread.values() for turn in turns]
    approvals = [approval.model_dump() for approval in db.approvals.values() if approval.status == "pending"]
    return {
        "serverTime": utc_now(),
        "snapshot": {
            "threads": [thread.model_dump() for thread in db.list_threads()],
            "turns": [turn.model_dump() for turn in turns],
            "pendingApprovals": approvals,
        },
        "events": [event.model_dump() for event in events],
        "lastEventId": events[-1].eventId if events else afterEventId or 0,
    }


@app.get("/api/threads")
async def list_threads(_: str = Depends(require_token)) -> dict[str, Any]:
    db: InMemoryDb = app.state.db
    return {"threads": [thread.model_dump() for thread in db.list_threads()]}


@app.post("/api/threads")
async def create_thread(payload: CreateThreadRequest, _: str = Depends(require_token)) -> dict[str, Any]:
    manager: FakeCodexManager = app.state.manager
    thread = await manager.create_thread(payload.title)
    return {"thread": thread.model_dump()}


@app.get("/api/threads/{thread_id}")
async def get_thread(thread_id: str, _: str = Depends(require_token)) -> dict[str, Any]:
    db: InMemoryDb = app.state.db
    return {
        "thread": db.get_thread(thread_id).model_dump(),
        "turns": [turn.model_dump() for turn in db.get_turns(thread_id)],
    }


@app.get("/api/threads/{thread_id}/events")
async def get_thread_events(
    thread_id: str,
    afterEventId: int | None = Query(default=None),
    limit: int = Query(default=500, le=5000),
    _: str = Depends(require_token),
) -> dict[str, Any]:
    db: InMemoryDb = app.state.db
    db.get_thread(thread_id)
    events = [
        event.model_dump()
        for event in db.events
        if event.threadId == thread_id and (afterEventId is None or event.eventId > afterEventId)
    ][:limit]
    return {"events": events, "lastEventId": events[-1]["eventId"] if events else afterEventId or 0}


@app.post("/api/threads/{thread_id}/turns")
async def post_turn(thread_id: str, payload: StartTurnRequest, _: str = Depends(require_token)) -> dict[str, Any]:
    manager: FakeCodexManager = app.state.manager
    turn = await manager.start_turn(thread_id, payload.text)
    return {"turn": turn.model_dump()}


@app.post("/api/threads/{thread_id}/fork")
async def post_fork(thread_id: str, payload: ForkThreadRequest, _: str = Depends(require_token)) -> dict[str, Any]:
    manager: FakeCodexManager = app.state.manager
    thread = await manager.fork_thread(thread_id, payload.title)
    return {"thread": thread.model_dump()}


@app.post("/api/approvals/{approval_id}")
async def respond_approval(approval_id: str, payload: ApprovalDecisionRequest, _: str = Depends(require_token)) -> dict[str, Any]:
    db: InMemoryDb = app.state.db
    approval = db.approvals.get(approval_id)
    if not approval:
        raise ApiError(404, "approval_not_found", f"Unknown approval: {approval_id}")
    if payload.decision not in {"approve", "deny"}:
        raise ApiError(400, "invalid_request", "Decision must be 'approve' or 'deny'")
    approval.status = payload.decision
    await app.state.ws.broadcast({"type": "approval.responded", "approval": approval.model_dump()})
    return {"approvalId": approval.approvalId, "decision": payload.decision, "status": "submitted"}


@app.post("/api/import/preview")
async def import_preview(payload: ImportPreviewRequest, _: str = Depends(require_token)) -> dict[str, Any]:
    manager: FakeCodexManager = app.state.manager
    return await manager.create_preview(payload)


@app.post("/api/import/commit")
async def import_commit(payload: ImportCommitRequest, _: str = Depends(require_token)) -> dict[str, Any]:
    manager: FakeCodexManager = app.state.manager
    return await manager.commit_preview(payload)


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str,
    lastEventId: int | None = None,
) -> None:
    db: InMemoryDb = app.state.db
    ws: WebSocketHub = app.state.ws
    await ws.connect(websocket, token)
    if websocket.client_state.name != "CONNECTED":
        return

    await ws.send_json(websocket, {"type": "connected", "serverTime": utc_now(), "replayFromEventId": lastEventId or 0})
    await ws.send_json(
        websocket,
        {
            "type": "snapshot",
            "snapshot": {
                "threads": [thread.model_dump() for thread in db.list_threads()],
                "turns": [turn.model_dump() for turns in db.turns_by_thread.values() for turn in turns],
                "pendingApprovals": [approval.model_dump() for approval in db.approvals.values() if approval.status == "pending"],
            },
        },
    )
    for event in db.events:
        if lastEventId is None or event.eventId > lastEventId:
            await ws.send_json(websocket, {"type": "replay.event", "event": event.model_dump()})
    await ws.send_json(websocket, {"type": "replay.complete", "lastEventId": db.events[-1].eventId if db.events else lastEventId or 0})

    try:
        while True:
            await websocket.receive_text()
    except Exception:
        await ws.disconnect(websocket)
```

```ts
// frontend/src/reference_contract_example.ts
//
// Public-scope reference only:
// - fetch wrapper with bearer token
// - reconnecting WebSocket with replay cursor
// - state shape for threads, turns, events, approvals
// - transcript delta buffering

export type ThreadRecord = {
  threadId: string;
  title: string | null;
  createdAt: string;
  updatedAt: string;
  parentThreadId: string | null;
  forkedFromTurnId: string | null;
  status: "idle" | "running" | "error";
  metadata: Record<string, unknown>;
};

export type TurnRecord = {
  turnId: string;
  threadId: string;
  idx: number;
  userText: string;
  status: "running" | "completed" | "error";
  startedAt: string;
  completedAt: string | null;
  metadata: Record<string, unknown>;
};

export type EventRecord = {
  eventId: number;
  threadId: string;
  turnId: string | null;
  seq: number;
  type: string;
  ts: string;
  payload: Record<string, unknown>;
};

export type ApprovalRecord = {
  approvalId: string;
  threadId: string;
  turnId: string;
  kind: string;
  details: Record<string, unknown>;
  status: "pending" | "approve" | "deny";
};

export type BootstrapResponse = {
  serverTime: string;
  snapshot: {
    threads: ThreadRecord[];
    turns: TurnRecord[];
    pendingApprovals: ApprovalRecord[];
  };
  events: EventRecord[];
  lastEventId: number;
};

type Store = {
  threads: Map<string, ThreadRecord>;
  turnsByThread: Map<string, TurnRecord[]>;
  eventsByTurn: Map<string, EventRecord[]>;
  approvals: Map<string, ApprovalRecord>;
  selectedThreadId: string | null;
  lastEventId: number;
  transcriptBuffers: Map<string, string>;
};

export const createStore = (): Store => ({
  threads: new Map(),
  turnsByThread: new Map(),
  eventsByTurn: new Map(),
  approvals: new Map(),
  selectedThreadId: null,
  lastEventId: 0,
  transcriptBuffers: new Map(),
});

export async function apiGet<T>(token: string, path: string): Promise<T> {
  const response = await fetch(path, {
    headers: {
      Authorization: `Bearer ${token}`,
    },
  });
  if (!response.ok) {
    throw new Error(`GET ${path} failed with ${response.status}`);
  }
  return (await response.json()) as T;
}

export async function apiPost<T>(token: string, path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`POST ${path} failed with ${response.status}`);
  }
  return (await response.json()) as T;
}

export function applyBootstrap(store: Store, payload: BootstrapResponse): void {
  for (const thread of payload.snapshot.threads) {
    store.threads.set(thread.threadId, thread);
  }
  for (const turn of payload.snapshot.turns) {
    const list = store.turnsByThread.get(turn.threadId) ?? [];
    list.push(turn);
    list.sort((a, b) => a.idx - b.idx);
    store.turnsByThread.set(turn.threadId, list);
  }
  for (const approval of payload.snapshot.pendingApprovals) {
    store.approvals.set(approval.approvalId, approval);
  }
  for (const event of payload.events) {
    applyEvent(store, event);
  }
  store.lastEventId = payload.lastEventId;
}

export function applyEvent(store: Store, event: EventRecord): void {
  const turnKey = `${event.threadId}:${event.turnId ?? "none"}`;
  const list = store.eventsByTurn.get(turnKey) ?? [];
  list.push(event);
  list.sort((a, b) => a.seq - b.seq);
  store.eventsByTurn.set(turnKey, list);
  store.lastEventId = Math.max(store.lastEventId, event.eventId);

  if (event.type === "item/agentMessage/delta" && event.turnId) {
    const existing = store.transcriptBuffers.get(turnKey) ?? "";
    const next = existing + String(event.payload.delta ?? "");
    store.transcriptBuffers.set(turnKey, next);
  }
}

export async function connectEventStream(token: string, store: Store): Promise<WebSocket> {
  const url = new URL("ws://127.0.0.1:8787/ws");
  url.searchParams.set("token", token);
  url.searchParams.set("lastEventId", String(store.lastEventId));
  const socket = new WebSocket(url.toString());

  socket.onmessage = (message) => {
    const data = JSON.parse(message.data as string);

    if (data.type === "snapshot") {
      for (const thread of data.snapshot.threads as ThreadRecord[]) {
        store.threads.set(thread.threadId, thread);
      }
      for (const turn of data.snapshot.turns as TurnRecord[]) {
        const list = store.turnsByThread.get(turn.threadId) ?? [];
        list.push(turn);
        list.sort((a, b) => a.idx - b.idx);
        store.turnsByThread.set(turn.threadId, list);
      }
      for (const approval of data.snapshot.pendingApprovals as ApprovalRecord[]) {
        store.approvals.set(approval.approvalId, approval);
      }
      return;
    }

    if (data.type === "replay.event" || data.type === "event") {
      applyEvent(store, data.event as EventRecord);
      return;
    }

    if (data.type === "thread.created" || data.type === "thread.forked") {
      const thread = data.thread as ThreadRecord;
      store.threads.set(thread.threadId, thread);
      return;
    }

    if (data.type === "turn.updated") {
      const turn = data.turn as TurnRecord;
      const list = store.turnsByThread.get(turn.threadId) ?? [];
      const filtered = list.filter((item) => item.turnId !== turn.turnId);
      filtered.push(turn);
      filtered.sort((a, b) => a.idx - b.idx);
      store.turnsByThread.set(turn.threadId, filtered);
      return;
    }

    if (data.type === "approval.requested" || data.type === "approval.responded") {
      const approval = data.approval as ApprovalRecord;
      store.approvals.set(approval.approvalId, approval);
    }
  };

  socket.onclose = () => {
    window.setTimeout(() => {
      void connectEventStream(token, store);
    }, 1000);
  };

  return socket;
}

export function buildTranscriptBlocks(store: Store, threadId: string, turnId: string): string[] {
  const key = `${threadId}:${turnId}`;
  const events = store.eventsByTurn.get(key) ?? [];
  const blocks: string[] = [];
  let bufferedDelta = "";

  for (const event of events) {
    if (event.type === "item/agentMessage/delta") {
      bufferedDelta += String(event.payload.delta ?? "");
      continue;
    }
    if (bufferedDelta) {
      blocks.push(bufferedDelta);
      bufferedDelta = "";
    }
    blocks.push(`${event.type}: ${JSON.stringify(event.payload)}`);
  }

  if (bufferedDelta) {
    blocks.push(bufferedDelta);
  }

  return blocks;
}
```

END
Status note as of 2026-03-03
----------------------------
This file is the original implementation plan and reference design.

The current implemented state differs in one important way:

- the frontend was built as static HTML/CSS/ES modules served by FastAPI, not React/Vite, because Node/NPM were unavailable in the workspace

For current runtime status, validated flows, and resume guidance, read `CONTEXT_DUMP.md` first.
