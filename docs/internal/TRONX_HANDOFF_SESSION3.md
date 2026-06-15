# TRON-X Project -- Session 3 Handoff Document
## All 20 Phases + Post-launch Fixes Complete

---

## Quick Start

```bash
cd D:\Tron_X
uvicorn src.main:app --reload
# Open http://127.0.0.1:8000
```

---

## Session 3 Changes

### HUD Redesign -- Chat-Centric Layout

**Problem:** Static 6-panel grid cluttered the screen, AI response text was invisible (CSS truncation bug), analytics counts never showed.

**What changed:**

#### `static/index.html`
- Removed 6-panel grid entirely
- New 2-column layout: large chat panel (flex:1) + dynamic info card (320px, hidden by default)
- Info card has a close button and appears only when triggered

#### `static/css/hud.css` (full rewrite via heredoc)
- New layout styles for `.hud-main` (flex), `.panel-chat`, `.info-card-area`, `.info-card`
- **Fixed text visibility**: `.msg-ai .msg-body { color: #c8f0ff }`, `.msg-user .msg-body { color: #ffbb88 }`
- Data card styles: `.card-big-val`, `.card-big-label`, `.data-table`, `.card-positive/negative`
- System card grid: `.sys-card-grid`, `.sys-card-stat`
- PTT recording animation: `@keyframes pulse-red`
- Responsive breakpoints at 900px and 640px

#### `static/js/panels.js` (full rewrite via heredoc)
- **Intent detection** (`detectIntent(msg)`) -- keyword regex on user message before sending:
  - Weather: weather/temperature/forecast/rain/snow/wind/humid/sunny/cold/hot
  - Crypto: bitcoin/btc/ethereum/eth/doge/sol/bnb/xrp/cardano/ada/ltc
  - Stocks: stock/share/equity/invest/trading/TICKER (2-5 uppercase letters)
  - News: news/headlines/latest/current events/breaking
  - System: cpu/ram/memory/disk/system stats/performance/processor/usage
- **Card system**: `showCard(title, renderFn, pollMs?)` / `hideCard()`
  - Crypto and stocks auto-refresh (30s and 60s respectively)
  - System stats polls every 4s while card is open
  - Weather and news are one-shot
- **Location/query extraction**: regex pulls city from "weather in X", coin from message, ticker from uppercase pattern
- **PTT voice**: MediaRecorder -> /api/voice/stream SSE; transcript also triggers intent detection
- Removed all static polling (old system/analytics/IoT/agents/memory panels gone)

---

## Bug Fixes Applied This Session

| Bug | Fix |
|---|---|
| AI/JARVIS text invisible | CSS file was truncated by Edit tool (box-drawing chars). Full rewrite via heredoc fixed colors. |
| Analytics showing -- | Nested response shape: `d.requests.total` not `d.total_requests` |
| IoT stuck on "SCANNING..." | Added NOT CONFIGURED state in catch + non-ok response |
| Plugin scan button missing | Added to agents panel with loading state |
| CSS truncated by Edit tool | All future CSS/JS edits with box-drawing chars MUST use bash heredoc |

---

## PERMANENT Workflow Rules

### 1. NEVER use Edit tool on files with box-drawing chars (U+2500 `--`)
All static files (hud.css, index.html, panels.js) and most Python files use `--` in comments. Always write via bash heredoc:
```bash
cat > /tmp/file.ext << 'ENDOFFILE'
...content...
ENDOFFILE
cp /tmp/file.ext /sessions/*/mnt/Tron_X/path/to/file.ext
```

### 2. Verify after every write
```bash
# JS:
node --check /sessions/*/mnt/Tron_X/static/js/panels.js && echo OK
# Python:
python3 -c "import ast; ast.parse(open('/tmp/file.py').read()); print('OK')"
```

### 3. Bash path this session
- `D:\Tron_X\` -> `/sessions/laughing-serene-tesla/mnt/Tron_X/`
- Outputs -> `/sessions/laughing-serene-tesla/mnt/outputs/`

### 4. Always `ls /sessions/` first in a new session to find the current mount path

---

## Current HUD Architecture

```
GET /  ->  static/index.html
           |
           +-- hud.js        (Three.js background: particle grid + torus rings)
           +-- panels.js     (all chat + card logic)
           +-- hud.css       (cyberpunk theme)

Layout:
  [topbar: logo | clock | status | persona | model]
  [hud-main: flex row]
    [panel-chat: flex:1]        <- always visible, full height
    [info-card-area: 320px]     <- hidden by default, .visible class shows it
      [info-card.panel]
        [panel-header: card-title | X CLOSE]
        [card-body]             <- weather / crypto / stocks / news / system
```

## Intent -> Card Mapping

| User says | Card shown | Auto-refresh |
|---|---|---|
| "weather in Mumbai" | WEATHER / MUMBAI | No (TTL cached server-side) |
| "bitcoin price" | CRYPTO / BITCOIN | Every 30s |
| "AAPL stock" | STOCKS / AAPL | Every 60s |
| "latest news about AI" | NEWS / AI | No |
| "cpu usage" / "system stats" | SYSTEM MONITOR | Every 4s |

---

## What's Left (future work)

- **IoT card**: "turn on lights" -> show IoT device panel (NL mapper already wired, just needs card UI)
- **Multi-card**: Currently one card at a time; could stack cards
- **Voice TTS playback**: PTT sends audio + receives SSE stream, but audio_chunk playback needs Web Audio API
- **Analytics card**: "show analytics" -> stats card
- **Memory search card**: "what did I say about X" -> episodic memory results card

---

*Generated: 2026-06-08 -- TRON-X Session 3 -- HUD redesign + bug fixes*
