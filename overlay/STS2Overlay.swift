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

class OverlayWindow: NSWindow {
    private var trackingTimer: Timer?
    private var isManuallyMoved = false
    let overlayWidth: CGFloat = 300
    let overlayMargin: CGFloat = 8

    init() {
        // 기본 위치 (게임 창을 찾으면 자동 조정)
        let screen = NSScreen.main!
        let width: CGFloat = 300
        let height: CGFloat = 500
        let x = screen.frame.maxX - width - 20
        let y: CGFloat = 100

        super.init(
            contentRect: NSRect(x: x, y: y, width: width, height: height),
            styleMask: [.borderless, .resizable],
            backing: .buffered,
            defer: false
        )

        self.isMovableByWindowBackground = true
        self.level = .floating
        self.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        self.backgroundColor = NSColor.clear
        self.isOpaque = false
        self.hasShadow = true

        // 모서리 둥글게
        self.contentView?.wantsLayer = true
        self.contentView?.layer?.cornerRadius = 12
        self.contentView?.layer?.masksToBounds = true

        // 게임 창 위치 추적 시작
        snapToGameWindow()
        // 60fps 추적
        trackingTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in
            self?.snapToGameWindow()
        }
        RunLoop.current.add(trackingTimer!, forMode: .common)
    }

    private var lastFrame: NSRect = .zero
    private var contentHeight: CGFloat = 300

    func adjustHeight(_ height: CGFloat) {
        contentHeight = min(max(height, 100), 600)
        lastFrame = .zero  // force recalc
        snapToGameWindow()
    }

    func snapToGameWindow() {
        guard let game = findGameWindow() else { return }
        guard let screen = NSScreen.main else { return }

        let screenHeight = screen.frame.height
        let gameBottom = screenHeight - game.y - game.height

        let newX = game.x + game.width - overlayWidth - overlayMargin
        let newY = gameBottom + overlayMargin
        let newHeight = min(contentHeight, game.height - overlayMargin * 2)

        let newFrame = NSRect(x: newX, y: newY, width: overlayWidth, height: newHeight)
        // 위치가 변하지 않았으면 skip
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
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    background: transparent;
    color: #e0e0e0;
    font-size: 13px;
    padding: 6px;
    -webkit-user-select: none;
    cursor: default;
    overflow-y: auto;
    height: 100vh;
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

/* Card Recommendations */
.reward-panel {
    background: rgba(20, 15, 5, 0.9);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: 1px solid rgba(255,180,0,0.3);
    border-radius: 10px;
    padding: 10px 12px;
    margin-bottom: 6px;
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
    background: rgba(255,255,255,0.04);
    border-radius: 8px;
    padding: 8px 10px;
    margin-bottom: 5px;
    border-left: 3px solid rgba(255,255,255,0.15);
}
.card-rec.best {
    border-left-color: #fbbf24;
    background: rgba(251,191,36,0.08);
    box-shadow: 0 0 12px rgba(251,191,36,0.1);
}
.card-name {
    font-size: 14px;
    font-weight: 700;
    color: #fff;
}
.card-cost {
    display: inline-block;
    background: rgba(96,165,250,0.2);
    border: 1px solid rgba(96,165,250,0.3);
    border-radius: 4px;
    padding: 0 5px;
    font-size: 11px;
    font-weight: 600;
    color: #93c5fd;
    margin-right: 4px;
}
.card-rarity { font-size: 11px; color: #888; margin-left: 4px; }
.rarity-Rare { color: #fbbf24; }
.rarity-Uncommon { color: #38bdf8; }
.rarity-Common { color: #999; }

.card-desc {
    font-size: 11px;
    color: #999;
    margin-top: 4px;
    line-height: 1.4;
}
.card-score {
    font-size: 13px;
    font-weight: 800;
    float: right;
    margin-top: -20px;
}
.score-high { color: #4ade80; }
.score-mid { color: #facc15; }
.score-low { color: #888; }

.pick-label {
    text-align: center;
    font-size: 13px;
    font-weight: 800;
    color: #fbbf24;
    margin-top: 6px;
    padding: 6px;
    background: rgba(251,191,36,0.08);
    border: 1px solid rgba(251,191,36,0.2);
    border-radius: 6px;
    letter-spacing: 1px;
}

.status {
    font-size: 10px;
    color: #555;
    text-align: center;
    padding: 4px 0;
}
.status.connecting { color: #f97316; }
.status.ready { color: #4ade80; }

.waiting {
    text-align: center;
    color: #555;
    font-size: 12px;
    padding: 20px 0;
}
.scan-btn {
    display: block;
    width: 100%;
    margin-top: 6px;
    padding: 6px;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 6px;
    color: #999;
    font-size: 11px;
    cursor: pointer;
    text-align: center;
    transition: all 0.15s;
}
.scan-btn:hover {
    background: rgba(251,191,36,0.15);
    border-color: rgba(251,191,36,0.3);
    color: #fbbf24;
}
</style>
</head>
<body>
<div id="content">
    <div class="waiting">서버 연결 중...</div>
</div>

<script>
let ws;
let reconnectTimeout;

function connect() {
    ws = new WebSocket('ws://127.0.0.1:9999/ws');

    ws.onopen = () => {
        document.getElementById('content').innerHTML = '<div class="waiting">게임 대기 중...</div>';
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        render(data);
    };

    ws.onclose = () => {
        document.getElementById('content').innerHTML = '<div class="waiting">서버 연결 끊김. 재연결 중...</div>';
        reconnectTimeout = setTimeout(connect, 3000);
    };

    ws.onerror = () => {
        ws.close();
    };
}

function render(data) {
    let html = '';

    if (data.run) {
        const r = data.run;
        const hpPct = r.current_hp / r.max_hp;
        const hpClass = hpPct > 0.6 ? 'hp-high' : hpPct > 0.3 ? 'hp-mid' : 'hp-low';

        html += `<div class="panel">
            <div class="header">
                <span class="character">${r.character}</span>
                <span class="hp ${hpClass}">${r.current_hp}/${r.max_hp} HP</span>
            </div>
            <div class="meta">Act ${r.act} · Floor ${r.floor} · Gold ${r.gold} · ${r.seed}</div>
            <div class="section-title">Deck (${r.deck_size})</div>
            <div class="deck-grid">`;
        for (const card of r.deck) {
            const countStr = card.count > 1 ? `<span class="count"> ×${card.count}</span>` : '';
            html += `<span class="deck-card">${card.name}${countStr}</span>`;
        }
        html += `</div>
            <div class="section-title">Relics</div>
            <div class="relics">${r.relics.join(' · ')}</div>
        </div>`;
    } else {
        html += '<div class="waiting">' + (data.connected ? '런 대기 중...' : '게임을 찾는 중...') + '</div>';
    }

    // Recommendations
    if (data.recommendations && data.recommendations.length > 0) {
        html += '<div class="reward-panel">';
        html += '<div class="reward-title">⚔ 카드 보상</div>';

        data.recommendations.forEach((card, i) => {
            const isBest = i === data.best_idx;
            const scoreClass = card.score >= 3 ? 'score-high' : card.score >= 1.5 ? 'score-mid' : 'score-low';
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
                    <div class="card-desc" style="color:#888;font-size:10px;margin-top:2px">${(card.reasons||[]).join(' · ')}</div>
                </div>
            `;
        });

        if (data.best_idx >= 0) {
            const best = data.recommendations[data.best_idx];
            html += `<div class="pick-label">★ ${best.name}</div>`;
        }
        html += '</div>';
    }

    // 재인식 버튼 + 상태
    if (data.ocr_status !== '준비 완료') {
        html += `<div class="status connecting">${data.ocr_status}</div>`;
    }
    if (data.run) {
        html += `<button class="scan-btn" onclick="ws.send('scan')">🔍 화면 재인식</button>`;
    }

    document.getElementById('content').innerHTML = html;
    // 콘텐츠 높이를 Swift에 알림
    setTimeout(() => {
        const h = document.getElementById('content').scrollHeight;
        window.webkit.messageHandlers.resize.postMessage(h);
    }, 50);
}

connect();
</script>
</body>
</html>
"""

// MARK: - App Delegate

class ResizeHandler: NSObject, WKScriptMessageHandler {
    weak var window: OverlayWindow?

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let height = message.body as? CGFloat, height > 0,
              let window = window else { return }
        window.adjustHeight(height + 20)
    }
}

class AppDelegate: NSObject, NSApplicationDelegate {
    var window: OverlayWindow!
    var webView: WKWebView!
    var wsClient: WebSocketClient!
    var resizeHandler: ResizeHandler!

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = OverlayWindow()

        resizeHandler = ResizeHandler()
        resizeHandler.window = window

        // WebView 설정 (투명 배경)
        let config = WKWebViewConfiguration()
        config.userContentController.add(resizeHandler, name: "resize")
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")

        webView = WKWebView(frame: window.contentView!.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.setValue(false, forKey: "drawsBackground")
        webView.allowsMagnification = false

        window.contentView?.addSubview(webView)
        webView.loadHTMLString(overlayHTML, baseURL: nil)

        window.makeKeyAndOrderFront(nil)

        // 메뉴바에 아이콘 없이 실행
        NSApp.setActivationPolicy(.accessory)

        // 우클릭 메뉴
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "수동 스캔", action: #selector(manualScan), keyEquivalent: "s"))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "투명도 높이기", action: #selector(moreTransparent), keyEquivalent: "-"))
        menu.addItem(NSMenuItem(title: "투명도 낮추기", action: #selector(lessTransparent), keyEquivalent: "="))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "종료", action: #selector(quit), keyEquivalent: "q"))
        window.contentView?.menu = menu
    }

    @objc func manualScan() {
        webView.evaluateJavaScript("ws.send('scan')", completionHandler: nil)
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
        return true
    }
}

// MARK: - Main

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
