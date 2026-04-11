import Cocoa
import WebKit

// MARK: - WebSocket Client

class WebSocketClient: NSObject, URLSessionWebSocketDelegate {
    var webSocket: URLSessionWebSocketTask?
    var onMessage: ((String) -> Void)?
    private var session: URLSession!
    private var isConnected = false
    private var reconnectTimer: Timer?

    override init() {
        super.init()
        session = URLSession(configuration: .default, delegate: self, delegateQueue: .main)
    }

    func connect() {
        guard let url = URL(string: "ws://127.0.0.1:9999/ws") else { return }
        webSocket = session.webSocketTask(with: url)
        webSocket?.resume()
        receiveMessage()
    }

    func disconnect() {
        webSocket?.cancel(with: .goingAway, reason: nil)
        isConnected = false
    }

    func send(_ text: String) {
        webSocket?.send(.string(text)) { _ in }
    }

    private func receiveMessage() {
        webSocket?.receive { [weak self] result in
            switch result {
            case .success(let message):
                switch message {
                case .string(let text):
                    self?.onMessage?(text)
                default:
                    break
                }
                self?.receiveMessage()
            case .failure(_):
                self?.isConnected = false
                self?.scheduleReconnect()
            }
        }
    }

    private func scheduleReconnect() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
            self?.connect()
        }
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        isConnected = true
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        isConnected = false
        scheduleReconnect()
    }
}

// MARK: - Game Window Tracker

struct GameWindowInfo {
    var x: CGFloat
    var y: CGFloat
    var width: CGFloat
    var height: CGFloat
}

func findGameWindow() -> GameWindowInfo? {
    let windowList = CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]] ?? []
    for w in windowList {
        guard let owner = w["kCGWindowOwnerName"] as? String,
              let name = w["kCGWindowName"] as? String,
              owner == "Slay the Spire 2", name == "Slay the Spire 2",
              let bounds = w["kCGWindowBounds"] as? [String: Any],
              let x = bounds["X"] as? CGFloat,
              let y = bounds["Y"] as? CGFloat,
              let width = bounds["Width"] as? CGFloat,
              let height = bounds["Height"] as? CGFloat,
              width > 100, height > 100
        else { continue }
        return GameWindowInfo(x: x, y: y, width: width, height: height)
    }
    return nil
}

// MARK: - Overlay Window

class OverlayWindow: NSPanel {
    private var trackingTimer: Timer?

    init() {
        let screen = NSScreen.main!
        let width: CGFloat = 800
        let height: CGFloat = 600
        let x = screen.frame.midX - width / 2
        let y = screen.frame.midY - height / 2

        super.init(
            contentRect: NSRect(x: x, y: y, width: width, height: height),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )

        self.isFloatingPanel = true
        self.hidesOnDeactivate = false
        self.isMovableByWindowBackground = false
        self.ignoresMouseEvents = true
        self.level = .screenSaver
        self.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        self.backgroundColor = NSColor.clear
        self.isOpaque = false
        self.hasShadow = false

        self.contentView?.wantsLayer = true
        self.contentView?.layer?.backgroundColor = NSColor.clear.cgColor

        snapToGameWindow()
        trackingTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in
            self?.snapToGameWindow()
        }
        trackingTimer?.tolerance = 0
        RunLoop.current.add(trackingTimer!, forMode: .common)
    }

    private var lastFrame: NSRect = .zero

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    func snapToGameWindow() {
        guard let game = findGameWindow() else { return }

        // 게임 창이 있는 스크린 찾기 (멀티 모니터 지원)
        let gameCenter = NSPoint(x: game.x + game.width / 2, y: game.y + game.height / 2)
        let screen = NSScreen.screens.first(where: { NSMouseInRect(gameCenter, $0.frame, false) }) ?? NSScreen.main!

        let screenHeight = screen.frame.height + screen.frame.origin.y
        let gameBottom = screenHeight - game.y - game.height

        let newFrame = NSRect(x: game.x, y: gameBottom, width: game.width, height: game.height)
        if newFrame == lastFrame { return }
        lastFrame = newFrame
        self.setFrame(newFrame, display: true, animate: false)
    }
}

// MARK: - HTML Content

let overlayHTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    position: relative;
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    background: transparent;
    color: #e0e0e0;
    font-size: 13px;
    -webkit-user-select: none;
    overflow: hidden;
    width: 100vw;
    height: 100vh;
}

#overlay-root {
    position: relative;
    width: 100vw;
    height: 100vh;
}

.panel-shell {
    position: absolute;
    width: 300px;
    pointer-events: none;
    display: flex;
    flex-direction: column;
}

body.layout-mode .panel-shell {
    pointer-events: auto;
}

.drag-handle {
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 7px 10px;
    margin-bottom: 6px;
    border-radius: 9px;
    background: rgba(0, 0, 0, 0.72);
    border: 1px dashed rgba(255,255,255,0.18);
    color: #cbd5e1;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.6px;
    cursor: grab;
}

body.layout-mode .drag-handle {
    display: flex;
}

body.dragging .drag-handle {
    cursor: grabbing;
}

.drag-meta {
    color: #94a3b8;
    font-size: 10px;
    text-transform: uppercase;
}

.layout-hud {
    position: absolute;
    top: 12px;
    left: 12px;
    padding: 8px 10px;
    border-radius: 9px;
    background: rgba(15, 23, 42, 0.86);
    border: 1px solid rgba(148,163,184,0.22);
    color: #e2e8f0;
    font-size: 11px;
    font-weight: 600;
    pointer-events: none;
}

.panel {
    background: rgba(10, 10, 15, 0.85);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px;
    padding: 10px 12px;
    margin-bottom: 6px;
}

.header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 6px;
    border-bottom: 1px solid rgba(255,255,255,0.1);
    margin-bottom: 6px;
}
.character { font-size: 15px; font-weight: 700; color: #ffd700; }
.hp { font-size: 13px; font-weight: 600; }
.hp-high { color: #4ade80; }
.hp-mid { color: #facc15; }
.hp-low { color: #f87171; }
.meta { font-size: 11px; color: #777; }

.section-title {
    font-size: 10px;
    font-weight: 700;
    color: #666;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    margin: 6px 0 4px;
}

.deck-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 1px 6px;
}
.deck-card {
    font-size: 11px;
    color: #ccc;
    white-space: nowrap;
}
.deck-card .count { color: #666; font-size: 10px; }

.relics {
    font-size: 11px;
    color: #c4b5fd;
}

.reward-panel {
    background: rgba(20, 15, 5, 0.9);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: 1px solid rgba(255,180,0,0.3);
    border-radius: 10px;
    padding: 10px 12px;
    margin-bottom: 6px;
    overflow-y: auto;
    overscroll-behavior: contain;
}
.reward-title {
    font-size: 11px;
    font-weight: 700;
    color: #fbbf24;
    text-align: center;
    text-transform: uppercase;
    letter-spacing: 2px;
    margin-bottom: 8px;
}

.card-rec {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-radius: 6px;
    padding: 6px 8px;
    margin-bottom: 4px;
    border-left: 3px solid rgba(255,255,255,0.12);
    position: relative;
}
.card-rec.best {
    border-left-color: #fbbf24;
    background: rgba(251,191,36,0.08);
}
.card-left {
    display: flex;
    align-items: center;
    gap: 6px;
    flex: 1;
    min-width: 0;
}
.card-cost {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    background: rgba(96,165,250,0.2);
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
    color: #93c5fd;
    flex-shrink: 0;
}
.card-name {
    font-size: 13px;
    font-weight: 600;
    color: #ddd;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.card-rec.best .card-name { color: #fff; }
.card-right {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
}
.card-score {
    font-size: 13px;
    font-weight: 800;
}
.score-high { color: #4ade80; }
.score-mid { color: #facc15; }
.score-low { color: #888; }
.info-btn {
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: rgba(255,255,255,0.08);
    border: 1px solid rgba(255,255,255,0.15);
    color: #888;
    font-size: 10px;
    font-weight: 700;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    position: relative;
}
.info-btn:hover { background: rgba(255,255,255,0.15); color: #ccc; }
.tooltip {
    display: none;
    position: absolute;
    right: 28px;
    top: -4px;
    background: rgba(10,10,15,0.95);
    border: 1px solid rgba(255,255,255,0.15);
    border-radius: 8px;
    padding: 8px 10px;
    width: 220px;
    z-index: 100;
    box-shadow: 0 4px 16px rgba(0,0,0,0.5);
}
.info-btn:hover .tooltip { display: block; }
.tooltip-rarity { font-size: 10px; margin-bottom: 4px; }
.rarity-Rare { color: #fbbf24; }
.rarity-Uncommon { color: #38bdf8; }
.rarity-Common { color: #999; }
.tooltip-desc {
    font-size: 11px;
    color: #bbb;
    line-height: 1.4;
    margin-bottom: 4px;
}
.tooltip-reasons {
    font-size: 10px;
    color: #777;
    border-top: 1px solid rgba(255,255,255,0.08);
    padding-top: 4px;
}

.pick-label {
    text-align: center;
    font-size: 12px;
    font-weight: 700;
    color: #fbbf24;
    margin-top: 4px;
    padding: 4px;
    background: rgba(251,191,36,0.06);
    border-radius: 4px;
}

/* Event badges - 선택지 옆에 절대 위치 배치 */
.event-badge {
    position: absolute;
    display: flex;
    align-items: center;
    gap: 5px;
    background: rgba(0, 0, 0, 0.85);
    border: 2px solid rgba(196,181,253,0.4);
    border-radius: 8px;
    padding: 4px 10px;
    white-space: nowrap;
    z-index: 9999;
    transform: translateY(-50%);
}
.event-badge-best {
    border-color: #fbbf24;
    background: rgba(251,191,36,0.2);
    box-shadow: 0 0 15px rgba(251,191,36,0.3);
}
.event-badge-star {
    color: #fbbf24;
    font-size: 14px;
}
.event-badge-score {
    font-size: 15px;
    font-weight: 800;
    color: #fff;
}
.event-badge-reason {
    font-size: 11px;
    color: #ccc;
    max-width: 140px;
    overflow: hidden;
    text-overflow: ellipsis;
}

.map-marker-layer {
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 3;
}

.map-marker {
    position: absolute;
    width: 28px;
    height: 28px;
    transform: translate(-50%, -50%);
}

.map-marker-core {
    position: absolute;
    inset: 7px;
    border-radius: 999px;
    background: rgba(251, 191, 36, 0.98);
    box-shadow: 0 0 18px rgba(251, 191, 36, 0.58);
}

.map-marker-ring,
.map-marker-ring-2 {
    position: absolute;
    inset: 0;
    border-radius: 999px;
    border: 2px solid rgba(251, 191, 36, 0.95);
    animation: mapPulse 1.7s ease-out infinite;
}

.map-marker-ring-2 {
    animation-delay: 0.85s;
}

.map-marker-label {
    position: absolute;
    left: 50%;
    bottom: 34px;
    transform: translateX(-50%);
    white-space: nowrap;
    padding: 4px 8px;
    border-radius: 999px;
    background: rgba(20, 12, 4, 0.94);
    border: 1px solid rgba(251, 191, 36, 0.55);
    color: #fbbf24;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.4px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.28);
}

.choice-marker-layer {
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 4;
}

.choice-marker {
    position: absolute;
    display: flex;
    align-items: center;
    gap: 8px;
    transform: translateY(-50%);
}

.choice-marker-line {
    width: 26px;
    height: 2px;
    border-radius: 999px;
    background: linear-gradient(90deg, rgba(251, 191, 36, 0.25), rgba(251, 191, 36, 0.95));
    box-shadow: 0 0 12px rgba(251, 191, 36, 0.28);
}

.choice-marker-pill {
    white-space: nowrap;
    padding: 5px 10px;
    border-radius: 999px;
    background: rgba(20, 12, 4, 0.96);
    border: 1px solid rgba(251, 191, 36, 0.55);
    color: #fbbf24;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.4px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.28);
}

.choice-marker.secondary .choice-marker-pill {
    color: #fde68a;
    border-color: rgba(253, 230, 138, 0.45);
}

.map-scroll-hud {
    position: absolute;
    top: 116px;
    right: 18px;
    width: 96px;
    padding: 8px 10px;
    border-radius: 12px;
    background: rgba(12, 18, 27, 0.88);
    border: 1px solid rgba(148, 163, 184, 0.22);
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.32);
    pointer-events: none;
    z-index: 4;
}

.map-scroll-title {
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: #e2e8f0;
    margin-bottom: 8px;
}

.map-scroll-track {
    position: relative;
    width: 10px;
    height: 112px;
    margin: 0 auto 8px;
    border-radius: 999px;
    background: linear-gradient(180deg, rgba(71, 85, 105, 0.88), rgba(15, 23, 42, 0.96));
    border: 1px solid rgba(148, 163, 184, 0.18);
}

.map-scroll-dot {
    position: absolute;
    left: 50%;
    width: 16px;
    height: 16px;
    border-radius: 999px;
    transform: translate(-50%, -50%);
    box-shadow: 0 0 12px rgba(0, 0, 0, 0.25);
}

.map-scroll-dot.anchor {
    background: #f59e0b;
    border: 2px solid rgba(255, 251, 235, 0.9);
}

.map-scroll-dot.target {
    background: #38bdf8;
    border: 2px solid rgba(224, 242, 254, 0.9);
}

.map-scroll-row {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    font-size: 10px;
    color: #cbd5e1;
    margin-top: 3px;
}

.map-scroll-key {
    color: #94a3b8;
}

.map-scroll-value {
    font-weight: 700;
    color: #f8fafc;
}

@keyframes mapPulse {
    0% {
        transform: scale(0.7);
        opacity: 0.95;
    }
    100% {
        transform: scale(1.95);
        opacity: 0;
    }
}

.placeholder-panel {
    background: rgba(8, 12, 18, 0.42);
    border: 1px dashed rgba(255,255,255,0.16);
    border-radius: 10px;
    padding: 14px 12px;
    color: #94a3b8;
    overflow-y: auto;
}

.placeholder-title {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.4px;
    margin-bottom: 6px;
    color: #e2e8f0;
}

.placeholder-body {
    font-size: 11px;
    line-height: 1.5;
}

.combat-panel {
    background: rgba(5, 15, 20, 0.9);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: 1px solid rgba(56,189,248,0.3);
    border-radius: 10px;
    padding: 10px 12px;
    margin-bottom: 6px;
    overflow-y: auto;
    overscroll-behavior: contain;
}
.combat-title {
    font-size: 11px;
    font-weight: 700;
    color: #38bdf8;
    text-align: center;
    text-transform: uppercase;
    letter-spacing: 2px;
    margin-bottom: 8px;
}
.combat-tip {
    font-size: 12px;
    color: #e0e0e0;
    padding: 5px 8px;
    margin-bottom: 3px;
    background: rgba(56,189,248,0.06);
    border-left: 2px solid rgba(56,189,248,0.4);
    border-radius: 4px;
    line-height: 1.4;
}
</style>
</head>
<body>
<div id="event-badges" style="position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:999;"></div>
<div id="overlay-root"></div>

<script>
let ws;
let reconnectTimeout;
let currentData = {};
let layoutMode = false;
let panelLayouts = {};
let dragState = null;

const PANEL_DEFAULTS = {
    reward: { title: '카드 보상', x: 0.74, y: 0.04, width: 300 },
    event: { title: '이벤트 선택', x: 0.74, y: 0.34, width: 300 },
    combat: { title: '전투 조언', x: 0.74, y: 0.64, width: 300 },
    shop: { title: '상점 추천', x: 0.72, y: 0.30, width: 320 },
    map: { title: '맵 경로', x: 0.74, y: 0.34, width: 320 },
};

function clamp(value, lower, upper) {
    return Math.max(lower, Math.min(upper, value));
}

function nativePost(name, payload) {
    try {
        window.webkit.messageHandlers[name].postMessage(payload);
    } catch (e) {
        // no-op
    }
}

function getPanelLayout(panelId) {
    const fallback = PANEL_DEFAULTS[panelId] || { title: panelId, x: 0.74, y: 0.04, width: 300 };
    const saved = panelLayouts[panelId] || {};
    return {
        x: typeof saved.x === 'number' ? clamp(saved.x, 0, 1) : fallback.x,
        y: typeof saved.y === 'number' ? clamp(saved.y, 0, 1) : fallback.y,
        width: typeof saved.width === 'number' ? clamp(saved.width, 240, 380) : fallback.width,
    };
}

function applyNativeLayout(layout) {
    if (!layout || typeof layout !== 'object') return;
    panelLayouts = { ...panelLayouts, ...layout };
    render(currentData);
}

function setLayoutMode(enabled) {
    layoutMode = !!enabled;
    document.body.classList.toggle('layout-mode', layoutMode);
    render(currentData);
}

function resetLayout() {
    panelLayouts = {};
    persistLayouts();
    render(currentData);
}

function persistLayouts() {
    nativePost('layout', JSON.stringify(panelLayouts));
}

function shellStyle(panelId, heightEstimate = 210) {
    const layout = getPanelLayout(panelId);
    const width = layout.width;
    const left = clamp(layout.x * window.innerWidth, 8, Math.max(8, window.innerWidth - width - 8));
    const top = clamp(layout.y * window.innerHeight, 8, Math.max(8, window.innerHeight - heightEstimate - 8));
    return `left:${left}px;top:${top}px;width:${width}px;`;
}

function fitShellToViewport(shell, panelId, persist = false) {
    if (!shell) return;

    const width = shell.offsetWidth || parseFloat(shell.style.width) || 300;
    const height = shell.offsetHeight || 220;
    const left = clamp(shell.offsetLeft, 8, Math.max(8, window.innerWidth - width - 8));
    const top = clamp(shell.offsetTop, 8, Math.max(8, window.innerHeight - Math.min(height, window.innerHeight - 16) - 8));

    shell.style.left = `${left}px`;
    shell.style.top = `${top}px`;

    const handle = shell.querySelector('.drag-handle');
    const handleHeight = handle ? handle.offsetHeight + 6 : 0;
    const maxHeight = Math.max(120, window.innerHeight - top - 8);
    shell.style.maxHeight = `${maxHeight}px`;

    const content = shell.querySelector('.reward-panel, .event-panel, .combat-panel, .placeholder-panel');
    if (content) {
        content.style.maxHeight = `${Math.max(100, maxHeight - handleHeight)}px`;
    }

    if (persist) {
        panelLayouts[panelId] = {
            ...getPanelLayout(panelId),
            x: left / Math.max(window.innerWidth, 1),
            y: top / Math.max(window.innerHeight, 1),
            width,
        };
    }
}

function fitPanelsToViewport(persist = false) {
    document.querySelectorAll('.panel-shell').forEach((shell) => {
        const panelId = shell.dataset.panel;
        if (!panelId) return;
        fitShellToViewport(shell, panelId, persist);
    });
    if (persist) {
        persistLayouts();
    }
}

function placeholderShell(title) {
    return `
        <div class="placeholder-panel">
            <div class="placeholder-title">${title}</div>
            <div class="placeholder-body">레이아웃 모드에서 드래그로 위치를 조정할 수 있습니다.</div>
        </div>
    `;
}

function wrapShell(panelId, title, visible, innerHTML, heightEstimate = 210) {
    if (!visible && !layoutMode) return '';
    return `
        <div class="panel-shell" data-panel="${panelId}" style="${shellStyle(panelId, heightEstimate)}">
            <div class="drag-handle" onmousedown="startDrag(event, '${panelId}')">
                <span>${title}</span>
                <span class="drag-meta">drag</span>
            </div>
            ${visible ? innerHTML : placeholderShell(title)}
        </div>
    `;
}

function startDrag(event, panelId) {
    if (!layoutMode) return;
    const shell = event.target.closest('.panel-shell');
    if (!shell) return;
    dragState = {
        panelId,
        offsetX: event.clientX - shell.offsetLeft,
        offsetY: event.clientY - shell.offsetTop,
    };
    document.body.classList.add('dragging');
    event.preventDefault();
}

window.startDrag = startDrag;
window.applyNativeLayout = applyNativeLayout;
window.setLayoutMode = setLayoutMode;
window.resetLayout = resetLayout;
window.requestLiveScan = function requestLiveScan() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send('scan');
    }
};

window.addEventListener('mousemove', (event) => {
    if (!dragState) return;
    const shell = document.querySelector(`.panel-shell[data-panel="${dragState.panelId}"]`);
    if (!shell) return;

    const width = shell.offsetWidth;
    const height = shell.offsetHeight;
    const left = clamp(event.clientX - dragState.offsetX, 8, Math.max(8, window.innerWidth - width - 8));
    const top = clamp(event.clientY - dragState.offsetY, 8, Math.max(8, window.innerHeight - height - 8));

    shell.style.left = `${left}px`;
    shell.style.top = `${top}px`;
    panelLayouts[dragState.panelId] = {
        ...getPanelLayout(dragState.panelId),
        x: left / Math.max(window.innerWidth, 1),
        y: top / Math.max(window.innerHeight, 1),
        width,
    };
    fitShellToViewport(shell, dragState.panelId, false);
});

window.addEventListener('mouseup', () => {
    if (!dragState) return;
    dragState = null;
    document.body.classList.remove('dragging');
    persistLayouts();
});

window.addEventListener('resize', () => {
    if (layoutMode || currentData) {
        render(currentData);
    }
});

function connect() {
    ws = new WebSocket('ws://127.0.0.1:9999/ws');

    ws.onopen = () => {
        render(currentData);
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        render(data);
    };

    ws.onclose = () => {
        render({});
        reconnectTimeout = setTimeout(connect, 3000);
    };

    ws.onerror = () => {
        ws.close();
    };
}

function rewardPanel(data) {
    const isRemoval = (data.choice_prompt || '').indexOf('제거') >= 0;
    const pickCount = data.choice_pick_count || 1;
    let html = '<div class="reward-panel">';
    html += `<div class="reward-title">${isRemoval ? '카드 제거' : '카드 보상'}</div>`;
    if (data.choice_prompt) {
        html += `<div class="event-match">${data.choice_prompt}</div>`;
    }

    // 제거 모드: 점수 낮은 순 정렬 → 낮은 카드 제거 추천
    let recs = [...(data.recommendations || [])];
    if (isRemoval) {
        recs.sort((a, b) => a.score - b.score);
    }
    // 추천 인덱스: 제거면 앞 N개(점수 낮은), 보상이면 기존 bestIndices
    let bestIndices;
    if (isRemoval) {
        bestIndices = new Set(Array.from({length: pickCount}, (_, i) => i));
    } else {
        bestIndices = new Set(
            Array.isArray(data.reward_best_indices) && data.reward_best_indices.length
                ? data.reward_best_indices
                : (typeof data.best_idx === 'number' && data.best_idx >= 0 ? [data.best_idx] : [])
        );
        recs = data.recommendations || [];
    }
    recs.forEach((card, i) => {
        const isBest = bestIndices.has(i);
        const scoreClass = card.score >= 3 ? 'score-high' : card.score >= 1.5 ? 'score-mid' : 'score-low';
        const rarityClass = 'rarity-' + (card.rarity_key || '');
        const desc = (card.description || '').replace(/\\n/g, ' ');
        const reasons = (card.reasons || []).join(' · ');
        html += `
            <div class="card-rec ${isBest ? 'best' : ''}">
                <div class="card-left">
                    <span class="card-cost">${card.cost}</span>
                    <span class="card-name">${isBest ? '★ ' : ''}${card.name}</span>
                </div>
                <div class="card-right">
                    <span class="card-score ${scoreClass}">${card.score.toFixed(1)}</span>
                    <div class="info-btn">i
                        <div class="tooltip">
                            <div class="tooltip-rarity ${rarityClass}">${card.rarity} · ${card.type || ''}</div>
                            <div class="tooltip-desc">${desc}</div>
                            <div class="tooltip-reasons">${reasons}</div>
                        </div>
                    </div>
                </div>
            </div>
        `;
    });
    if (bestIndices.size) {
        const picks = Array.from(bestIndices)
            .sort((a, b) => a - b)
            .map((idx) => recs[idx]?.name)
            .filter(Boolean);
        if (picks.length) {
            const label = isRemoval ? '제거 추천' : (pickCount > 1 ? '추천 선택' : '추천');
            html += `<div class="pick-label">★ ${label} ${picks.join(' / ')}</div>`;
        }
    }
    html += '</div>';
    return html;
}

function eventPanel(eventRec) {
    let html = '';
    eventRec.options.forEach((option, i) => {
        const isBest = i === eventRec.best_idx;
        const anchor = option.screen_anchor;
        if (!anchor) return;

        // data/ui_offsets.json에서 서버가 전달한 오프셋 사용
        const offsets = window._uiOffsets || {};
        const ox = offsets.event_badge_x || 30.4;
        const oy = offsets.event_badge_y || -0.1;
        const leftPct = (anchor.x * 100 + ox).toFixed(1);
        const topPct = (anchor.y * 100 + oy).toFixed(1);
        const scoreClass = option.score >= 3 ? 'score-high' : option.score >= 1.5 ? 'score-mid' : 'score-low';
        const reasons = (option.reasons || []).join(' · ');

        html += `
            <div class="event-badge ${isBest ? 'event-badge-best' : ''}" style="left:${leftPct}%;top:${topPct}%;">
                <span class="event-badge-star">${isBest ? '★' : ''}</span>
                <span class="event-badge-score ${scoreClass}">${option.score.toFixed(1)}</span>
                <span class="event-badge-reason">${reasons}</span>
            </div>
        `;
    });
    // 절대 위치 배지라 패널 닫기 불필요
    return html;
}

function combatPanel(tips) {
    let html = '<div class="combat-panel">';
    html += '<div class="combat-title">전투 조언</div>';
    const combatCards = currentData.combat_cards || [];
    const combatBestIdx = typeof currentData.combat_best_idx === 'number' ? currentData.combat_best_idx : -1;
    const combatSequence = currentData.combat_sequence || [];
    const run = currentData.run || {};
    const energyText = typeof run.max_energy === 'number' ? `에너지 ${run.max_energy}` : '';
    const starText = typeof currentData.current_stars === 'number' ? `별 ${currentData.current_stars}` : '';
    if (energyText || starText) {
        html += `<div class="combat-tip">${[energyText, starText].filter(Boolean).join(' · ')}</div>`;
    }
    const bestCard = combatBestIdx >= 0 ? combatCards[combatBestIdx] : null;
    if (bestCard && bestCard.target_label) {
        html += '<div class="section-title">집중 타깃</div>';
        html += `<div class="combat-tip">${bestCard.target_label}${bestCard.target_reason ? ' · ' + bestCard.target_reason : ''}</div>`;
    }
    if (combatSequence.length) {
        const sequenceLabel = combatSequence
            .map((item) => {
                const starCost = item.star_cost ? ` [★${item.star_cost}]` : '';
                return `${item.step}. ${item.name}${starCost}${item.target_label ? ' → ' + item.target_label : ''}`;
            })
            .join(' → ');
        html += '<div class="section-title">추천 순서</div>';
        html += `<div class="combat-tip">${sequenceLabel}</div>`;
    }
    if (combatCards.length) {
        html += '<div class="section-title">지금 낼 카드</div>';
        combatCards.forEach((card, i) => {
            const isBest = i === combatBestIdx;
            const scoreClass = card.score >= 4 ? 'score-high' : card.score >= 2 ? 'score-mid' : 'score-low';
            const rarityClass = 'rarity-' + card.rarity_key;
            html += `
                <div class="card-rec ${isBest ? 'best' : ''}">
                    <div>
                        <span class="card-cost">${card.cost}</span>
                        <span class="card-name">${card.name}</span>
                        <span class="card-rarity ${rarityClass}">${card.rarity}</span>
                        <span class="card-score ${scoreClass}">${card.score.toFixed(1)}</span>
                    </div>
                    <div class="card-desc">${card.description.replace(/\\n/g, ' ')}</div>
                    <div class="card-desc" style="color:#7dd3fc;font-size:10px;margin-top:2px">
                        매칭 ${card.match_pct}%${card.target_label ? ' · 대상 ' + card.target_label : ''} · ${(card.reasons || []).join(' · ')}
                    </div>
                </div>
            `;
        });
        if (combatBestIdx >= 0) {
            const best = combatCards[combatBestIdx];
            html += `<div class="pick-label">★ 이번 턴 우선 ${best.name}${best.target_label ? ' → ' + best.target_label : ''}</div>`;
        }
    }
    if (tips && tips.length) {
        html += '<div class="section-title">운영 팁</div>';
        tips.forEach((tip) => {
            html += '<div class="combat-tip">' + tip + '</div>';
        });
    }
    html += '</div>';
    return html;
}

function shopPanel(shopRec) {
    let html = '<div class="event-panel">';
    html += '<div class="event-title">상점 추천</div>';
    html += `<div class="event-match">보유 골드 ${shopRec.gold}G</div>`;
    if (shopRec.bundle && shopRec.bundle.items && shopRec.bundle.items.length) {
        html += `<div class="combat-tip">총 ${shopRec.bundle.total_cost}G · 남는 골드 ${shopRec.bundle.remaining_gold}G</div>`;
        shopRec.bundle.items.forEach((item) => {
            html += `
                <div class="event-option best">
                    <div class="event-option-title">${item.title}</div>
                    <div class="event-option-score">${item.score.toFixed(1)}</div>
                    <div class="event-option-desc">${item.kind_label} · ${item.price}G</div>
                </div>
            `;
        });
        const names = shopRec.bundle.items.map((item) => item.title).join(' + ');
        html += `<div class="pick-label">★ 한 번에 구매 ${names}</div>`;
    } else {
        const picks = (shopRec.items || []).filter((item) => item.affordable).slice(0, 3);
        picks.forEach((item) => {
            const priceText = typeof item.price === 'number' ? `${item.price}G` : '?';
            html += `
                <div class="event-option best">
                    <div class="event-option-title">${item.title}</div>
                    <div class="event-option-score">${item.score.toFixed(1)}</div>
                    <div class="event-option-desc">${item.kind_label} · ${priceText}</div>
                </div>
            `;
        });
        if (picks.length) {
            html += `<div class="pick-label">★ 우선 구매 ${picks.map((item) => item.title).join(' / ')}</div>`;
        }
    }
    html += '</div>';
    return html;
}

function mapPanel(mapRec) {
    let html = '<div class="event-panel">';
    html += '<div class="event-title">맵 경로</div>';
    mapRec.routes.forEach((route, i) => {
        const isBest = i === mapRec.best_idx;
        html += `
            <div class="event-option ${isBest ? 'best' : ''}">
                <div class="event-option-title">${route.next_label} (${route.next_coord.col},${route.next_coord.row})</div>
                <div class="event-option-score">${route.score.toFixed(1)}</div>
                <div class="event-option-desc">${route.summary}</div>
                <div class="card-desc" style="color:#8b7fb2;font-size:10px;margin-top:2px">${(route.reasons || []).join(' · ')}</div>
                <div class="card-desc" style="color:#6f88a3;font-size:10px;margin-top:2px">${(route.path_types || []).join(' → ')}</div>
            </div>
        `;
    });
    if (mapRec.best_idx >= 0) {
        const best = mapRec.routes[mapRec.best_idx];
        html += `<div class="pick-label">★ 다음: ${best.next_label}</div>`;
    }
    html += '</div>';
    return html;
}

function mapMarker(mapRec) {
    if (!mapRec || !mapRec.routes || !mapRec.routes.length) return '';
    const best = mapRec.routes[mapRec.best_idx || 0];
    if (!best || !best.next_coord || !mapRec.current_coord) return '';

    if (mapRec.target_screen && typeof mapRec.target_screen.x === 'number' && typeof mapRec.target_screen.y === 'number') {
        const x = clamp(mapRec.target_screen.x * window.innerWidth, 24, window.innerWidth - 24);
        const y = clamp(mapRec.target_screen.y * window.innerHeight, 32, window.innerHeight - 32);
        return `
            <div class="map-marker-layer">
                <div class="map-marker" style="left:${x}px; top:${y}px;">
                    <div class="map-marker-ring"></div>
                    <div class="map-marker-ring-2"></div>
                    <div class="map-marker-core"></div>
                    <div class="map-marker-label">추천</div>
                </div>
            </div>
        `;
    }
    return '';
}

function rewardMarkerAnchor(card, fallbackIndex) {
    if (card && card.screen_anchor && typeof card.screen_anchor.x === 'number' && typeof card.screen_anchor.y === 'number') {
        return card.screen_anchor;
    }

    const position = typeof card?.position === 'number' ? card.position : fallbackIndex;
    const defaultAnchors = [
        { x: 0.325, y: 0.52 },
        { x: 0.50, y: 0.52 },
        { x: 0.67, y: 0.52 },
    ];
    if (position >= 0 && position < defaultAnchors.length) {
        return defaultAnchors[position];
    }
    return null;
}

function rewardMarkers(data) {
    if (!data || !Array.isArray(data.recommendations) || !data.recommendations.length) return '';
    const bestIndices = Array.isArray(data.reward_best_indices) && data.reward_best_indices.length
        ? [...data.reward_best_indices].sort((a, b) => a - b)
        : (typeof data.best_idx === 'number' && data.best_idx >= 0 ? [data.best_idx] : []);
    if (!bestIndices.length) return '';

    let html = '<div class="choice-marker-layer">';
    bestIndices.forEach((idx, rank) => {
        const card = data.recommendations[idx];
        const anchor = rewardMarkerAnchor(card, idx);
        if (!card || !anchor) return;

        const x = clamp(anchor.x * window.innerWidth + 84, 42, window.innerWidth - 120);
        const y = clamp(anchor.y * window.innerHeight, 40, window.innerHeight - 40);
        const label = bestIndices.length > 1 ? `${rank + 1}픽` : '추천';
        html += `
            <div class="choice-marker ${rank > 0 ? 'secondary' : ''}" style="left:${x}px; top:${y}px;">
                <div class="choice-marker-line"></div>
                <div class="choice-marker-pill">${label}</div>
            </div>
        `;
    });
    html += '</div>';
    return html;
}

function eventMarkerAnchor(option, fallbackIndex, count) {
    if (option && option.screen_anchor && typeof option.screen_anchor.x === 'number' && typeof option.screen_anchor.y === 'number') {
        return option.screen_anchor;
    }

    const defaultY = [0.55, 0.66, 0.77, 0.86];
    const y = defaultY[Math.max(0, Math.min(defaultY.length - 1, fallbackIndex))] || 0.66;
    return {
        x: 0.89,
        y,
    };
}

function eventMarkers(eventRec) {
    if (!eventRec || !Array.isArray(eventRec.options) || !eventRec.options.length) return '';
    if (typeof eventRec.best_idx !== 'number' || eventRec.best_idx < 0 || eventRec.best_idx >= eventRec.options.length) return '';

    const option = eventRec.options[eventRec.best_idx];
    const anchor = eventMarkerAnchor(option, eventRec.best_idx, eventRec.options.length);
    if (!anchor) return '';

    const x = clamp(anchor.x * window.innerWidth - 96, 42, window.innerWidth - 120);
    const y = clamp(anchor.y * window.innerHeight, 40, window.innerHeight - 40);
    return `
        <div class="choice-marker-layer">
            <div class="choice-marker" style="left:${x}px; top:${y}px;">
                <div class="choice-marker-line"></div>
                <div class="choice-marker-pill">추천</div>
            </div>
        </div>
    `;
}

function mapScrollHud(mapRec) {
    if (!mapRec) return '';
    const anchor = mapRec.anchor_screen;
    const target = mapRec.target_screen;
    if ((!anchor || typeof anchor.y !== 'number') && (!target || typeof target.y !== 'number')) return '';

    const anchorTop = anchor && typeof anchor.y === 'number' ? clamp(anchor.y * 100, 0, 100) : null;
    const targetTop = target && typeof target.y === 'number' ? clamp(target.y * 100, 0, 100) : null;

    let dots = '';
    if (typeof targetTop === 'number') {
        dots += `<div class="map-scroll-dot target" style="top:${targetTop}%;"></div>`;
    }
    if (typeof anchorTop === 'number') {
        dots += `<div class="map-scroll-dot anchor" style="top:${anchorTop}%;"></div>`;
    }

    return `
        <div class="map-scroll-hud">
            <div class="map-scroll-title">스크롤</div>
            <div class="map-scroll-track">${dots}</div>
            ${typeof anchorTop === 'number' ? `<div class="map-scroll-row"><span class="map-scroll-key">앵커</span><span class="map-scroll-value">${anchorTop.toFixed(1)}%</span></div>` : ''}
            ${typeof targetTop === 'number' ? `<div class="map-scroll-row"><span class="map-scroll-key">다음</span><span class="map-scroll-value">${targetTop.toFixed(1)}%</span></div>` : ''}
        </div>
    `;
}

function render(data) {
    currentData = data || {};
    window._uiOffsets = data.ui_offsets || {};
    let html = '';
    let visiblePanels = 0;

    const hasReward = !!(data.recommendations && data.recommendations.length > 0);
    const hasEvent = !!(data.event_recommendation && data.event_recommendation.options && data.event_recommendation.options.length > 0);
    const hasCombat = !!(
        (data.combat_advice && data.combat_advice.length > 0)
        || (data.combat_cards && data.combat_cards.length > 0)
    );
    const hasShop = !!(data.shop_recommendation && data.shop_recommendation.items && data.shop_recommendation.items.length > 0);
    const hasMap = !!(data.map_recommendation && data.map_recommendation.routes && data.map_recommendation.routes.length > 0);
    const showMap = hasMap && !hasReward && !hasEvent && !hasCombat && !hasShop;

    if (layoutMode) {
        html += '<div class="layout-hud">레이아웃 모드 · 패널을 드래그해 배치하고 메뉴바에서 종료하세요.</div>';
    }

    if (showMap) {
        html += mapMarker(data.map_recommendation);
        html += mapScrollHud(data.map_recommendation);
    }
    if (hasReward) {
        html += rewardMarkers(data);
    }
    html += wrapShell('reward', '카드 보상', hasReward, hasReward ? rewardPanel(data) : '', 330);
    // 이벤트는 event-badges 레이어에서 처리 (wrapShell 제거)
    html += wrapShell('combat', '전투 조언', hasCombat, hasCombat ? combatPanel(data.combat_advice || []) : '', 380);
    html += wrapShell('shop', '상점 추천', hasShop, hasShop ? shopPanel(data.shop_recommendation) : '', 250);
    html += wrapShell('map', '맵 경로', showMap, showMap ? mapPanel(data.map_recommendation) : '', 340);

    if (hasReward) visiblePanels += 1;
    if (hasEvent) visiblePanels += 1;
    if (hasCombat) visiblePanels += 1;
    if (hasShop) visiblePanels += 1;
    if (showMap) visiblePanels += 1;

    document.getElementById('overlay-root').innerHTML = html;

    // 이벤트 배지를 고정 레이어에 렌더링
    if (hasEvent && data.event_recommendation) {
        document.getElementById('event-badges').innerHTML = eventPanel(data.event_recommendation);
    } else {
        document.getElementById('event-badges').innerHTML = '';
    }
    fitPanelsToViewport(false);
    nativePost('overlayState', JSON.stringify({
        visiblePanels: visiblePanels > 0,
        mapInteractive: showMap,
    }));
}

connect();
</script>
</body>
</html>
"""

// MARK: - App Delegate

class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var window: OverlayWindow!
    var webView: WKWebView!
    var wsClient: WebSocketClient!
    var statusItem: NSStatusItem!
    var showItem: NSMenuItem!
    var layoutItem: NSMenuItem!
    var webViewReady = false
    var isOverlaySuppressed = false
    var isLayoutMode = false
    var hasVisiblePanels = false
    var isMapInteractionMode = false
    var globalMapEventMonitor: Any?
    var lastGlobalMapScanUptime: TimeInterval = 0
    var hasScheduledMapScan = false
    let layoutStorageKey = "STS2OverlayPanelLayouts"
    let debugLogURL = URL(fileURLWithPath: "/tmp/sts2-overlay-debug.log")

    private func appendDebugLog(_ message: String) {
        let line = "[\(Date())] \(message)\n"
        guard let data = line.data(using: .utf8) else { return }
        if FileManager.default.fileExists(atPath: debugLogURL.path) {
            if let handle = try? FileHandle(forWritingTo: debugLogURL) {
                try? handle.seekToEnd()
                try? handle.write(contentsOf: data)
                try? handle.close()
            }
        } else {
            try? data.write(to: debugLogURL)
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        try? FileManager.default.removeItem(at: debugLogURL)
        appendDebugLog("applicationDidFinishLaunching")
        window = OverlayWindow()

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "overlayState")
        config.userContentController.add(self, name: "layout")
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")

        webView = WKWebView(frame: window.contentView!.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.setValue(false, forKey: "drawsBackground")
        webView.allowsMagnification = false
        webView.navigationDelegate = self

        window.contentView?.addSubview(webView)
        webView.loadHTMLString(overlayHTML, baseURL: nil)
        window.orderOut(nil)

        NSApp.setActivationPolicy(.accessory)

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "suit.spade.fill", accessibilityDescription: "STS2 Tracker")
            button.image?.size = NSSize(width: 16, height: 16)
        }

        let statusMenu = NSMenu()
        let scanItem = NSMenuItem(title: "화면 재인식", action: #selector(manualScan), keyEquivalent: "s")
        scanItem.target = self
        statusMenu.addItem(scanItem)
        statusMenu.addItem(NSMenuItem.separator())

        showItem = NSMenuItem(title: "오버레이 숨기기", action: #selector(toggleOverlay), keyEquivalent: "o")
        showItem.target = self
        statusMenu.addItem(showItem)
        layoutItem = NSMenuItem(title: "레이아웃 모드 켜기", action: #selector(toggleLayoutMode), keyEquivalent: "l")
        layoutItem.target = self
        statusMenu.addItem(layoutItem)
        let resetLayoutItem = NSMenuItem(title: "패널 위치 초기화", action: #selector(resetLayout), keyEquivalent: "r")
        resetLayoutItem.target = self
        statusMenu.addItem(resetLayoutItem)
        statusMenu.addItem(NSMenuItem.separator())
        let moreTransparentItem = NSMenuItem(title: "투명도 높이기", action: #selector(moreTransparent), keyEquivalent: "-")
        moreTransparentItem.target = self
        statusMenu.addItem(moreTransparentItem)
        let lessTransparentItem = NSMenuItem(title: "투명도 낮추기", action: #selector(lessTransparent), keyEquivalent: "=")
        lessTransparentItem.target = self
        statusMenu.addItem(lessTransparentItem)
        statusMenu.addItem(NSMenuItem.separator())
        let quitItem = NSMenuItem(title: "종료", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        statusMenu.addItem(quitItem)
        statusItem.menu = statusMenu

        let contextMenu = NSMenu()
        let contextScanItem = NSMenuItem(title: "수동 스캔", action: #selector(manualScan), keyEquivalent: "")
        contextScanItem.target = self
        contextMenu.addItem(contextScanItem)
        let contextLayoutItem = NSMenuItem(title: "레이아웃 모드 토글", action: #selector(toggleLayoutMode), keyEquivalent: "")
        contextLayoutItem.target = self
        contextMenu.addItem(contextLayoutItem)
        let contextResetItem = NSMenuItem(title: "위치 초기화", action: #selector(resetLayout), keyEquivalent: "")
        contextResetItem.target = self
        contextMenu.addItem(contextResetItem)
        contextMenu.addItem(NSMenuItem.separator())
        let contextQuitItem = NSMenuItem(title: "종료", action: #selector(quit), keyEquivalent: "")
        contextQuitItem.target = self
        contextMenu.addItem(contextQuitItem)
        window.contentView?.menu = contextMenu
        updateMenuState()
    }

    @objc func toggleOverlay() {
        isOverlaySuppressed.toggle()
        updateMenuState()
        updateGlobalMapMonitoring()
        updateWindowVisibility()
    }

    @objc func toggleLayoutMode() {
        isLayoutMode.toggle()
        if isLayoutMode {
            isOverlaySuppressed = false
        }
        updateInteractionMode()
        syncLayoutModeToWebView()
        updateMenuState()
        updateWindowVisibility()
    }

    @objc func resetLayout() {
        UserDefaults.standard.removeObject(forKey: layoutStorageKey)
        guard webViewReady else { return }
        webView.evaluateJavaScript("resetLayout()", completionHandler: nil)
    }

    @objc func manualScan() {
        webView.evaluateJavaScript("try { ws && ws.readyState === 1 && ws.send('scan') } catch(e) {}", completionHandler: nil)
    }

    @objc func moreTransparent() {
        window.alphaValue = max(0.3, window.alphaValue - 0.1)
    }

    @objc func lessTransparent() {
        window.alphaValue = min(1.0, window.alphaValue + 0.1)
    }

    @objc func quit() {
        NSApp.terminate(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        webViewReady = true
        appendDebugLog("webView didFinish")
        loadStoredLayout()
        syncLayoutModeToWebView()
        updateWindowVisibility()
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "overlayState" {
            if let payload = message.body as? String,
               let data = payload.data(using: .utf8),
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                hasVisiblePanels = (object["visiblePanels"] as? Bool) ?? false
                isMapInteractionMode = (object["mapInteractive"] as? Bool) ?? false
                appendDebugLog("overlayState visiblePanels=\(hasVisiblePanels) mapInteractive=\(isMapInteractionMode)")
            } else {
                hasVisiblePanels = (message.body as? Bool) ?? false
                isMapInteractionMode = false
                appendDebugLog("overlayState legacy visiblePanels=\(hasVisiblePanels)")
            }
            updateGlobalMapMonitoring()
            updateWindowVisibility()
            return
        }

        if message.name == "layout", let layoutJSON = message.body as? String {
            UserDefaults.standard.set(layoutJSON, forKey: layoutStorageKey)
        }
    }

    private func loadStoredLayout() {
        guard webViewReady else { return }
        guard let layoutJSON = UserDefaults.standard.string(forKey: layoutStorageKey) else { return }
        webView.evaluateJavaScript("applyNativeLayout(\(layoutJSON))", completionHandler: nil)
    }

    private func syncLayoutModeToWebView() {
        guard webViewReady else { return }
        let enabled = isLayoutMode ? "true" : "false"
        webView.evaluateJavaScript("setLayoutMode(\(enabled))", completionHandler: nil)
    }

    private func updateInteractionMode() {
        window.ignoresMouseEvents = !isLayoutMode
        updateGlobalMapMonitoring()
    }

    private func updateGlobalMapMonitoring() {
        let shouldMonitor = isMapInteractionMode && hasVisiblePanels && !isOverlaySuppressed && !isLayoutMode
        if shouldMonitor {
            startGlobalMapMonitoring()
        } else {
            stopGlobalMapMonitoring()
        }
    }

    private func startGlobalMapMonitoring() {
        guard globalMapEventMonitor == nil else { return }
        let mask: NSEvent.EventTypeMask = [.scrollWheel, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged]
        globalMapEventMonitor = NSEvent.addGlobalMonitorForEvents(matching: mask) { [weak self] _ in
            self?.handleGlobalMapInteraction()
        }
        requestGlobalMapScan(force: true)
    }

    private func stopGlobalMapMonitoring() {
        if let monitor = globalMapEventMonitor {
            NSEvent.removeMonitor(monitor)
            globalMapEventMonitor = nil
        }
        hasScheduledMapScan = false
    }

    private func handleGlobalMapInteraction() {
        guard isMapInteractionMode, !isLayoutMode else { return }
        guard window.frame.contains(NSEvent.mouseLocation) else { return }
        requestGlobalMapScan(force: false)
    }

    private func requestGlobalMapScan(force: Bool) {
        guard webViewReady, isMapInteractionMode, !isLayoutMode else { return }

        let now = ProcessInfo.processInfo.systemUptime
        let minInterval = 1.0 / 60.0
        if !force {
            let elapsed = now - lastGlobalMapScanUptime
            if elapsed < minInterval {
                if !hasScheduledMapScan {
                    hasScheduledMapScan = true
                    let delay = minInterval - elapsed
                    DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                        guard let self else { return }
                        self.hasScheduledMapScan = false
                        self.requestGlobalMapScan(force: true)
                    }
                }
                return
            }
        }

        lastGlobalMapScanUptime = now
        webView.evaluateJavaScript("window.requestLiveScan && window.requestLiveScan()", completionHandler: nil)
    }

    private func updateWindowVisibility() {
        // 게임이 실행 중이면 항상 표시 (배지/패널 여부 무관)
        let shouldShow = !isOverlaySuppressed
        if shouldShow {
            window.snapToGameWindow()
            window.orderFrontRegardless()
        } else {
            window.orderOut(nil)
        }
    }

    private func updateMenuState() {
        showItem.title = isOverlaySuppressed ? "오버레이 표시" : "오버레이 숨기기"
        layoutItem.title = isLayoutMode ? "레이아웃 모드 끄기" : "레이아웃 모드 켜기"
        layoutItem.state = isLayoutMode ? .on : .off
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopGlobalMapMonitoring()
    }
}

// MARK: - Main

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
