# TRON-X

**A self-hosted, multi-model AI assistant for Windows** — think Jarvis/Friday from Iron Man. TRON-X runs as a local FastAPI server, exposes a full REST API, and ships with a Three.js HUD frontend. It can be controlled by voice or natural language text, and autonomously delegates tasks to specialized sub-agents: web research, browser automation, smart-home control, email, calendar, code execution, screen vision, and more.

**Author:** Vishnu Sharma

---

## One-line pitch

A self-hosted, multi-model AI assistant that can see your screen, control your PC, talk to you, manage your calendar, and automate tasks — all from a local FastAPI server.

---

## Tech Stack

| Layer | Technology | Why |
|---|---|---|
| Web framework | FastAPI + Uvicorn | Async Python, auto-generated OpenAPI docs, fast |
| LLM routing | LiteLLM | Unified API for 100+ models across 14 providers (OpenAI, Anthropic, Groq, Gemini, etc.) |
| Vector memory | ChromaDB | Local vector database for semantic search / RAG |
| Embeddings | SentenceTransformers (`all-MiniLM-L6-v2`) | 384-dim, CPU-only |
| Cloud DB | Supabase | Optional persistent chat history |
| Voice STT | Groq Whisper API | Fast speech-to-text |
| Voice TTS | Kokoro-ONNX (local) → ElevenLabs → edge-tts → pyttsx3 | Cascading fallback chain |
| Browser control | Playwright (Chromium) | Headless browser automation |
| Scheduling | APScheduler | Cron jobs and one-shot reminders |
| IoT | Home Assistant REST API + MQTT | Smart home device control |
| Email | imaplib / smtplib | Read + send email |
| Calendar | Google Calendar API (OAuth2) | Event management |
| System control | psutil + pyautogui + PowerShell | OS-level automation on Windows |
| Screen/OCR | mss + Tesseract + EasyOCR | Screenshots and text extraction |
| Config | Pydantic Settings + python-dotenv | Type-safe env var loading |
| Containerisation | Docker + docker-compose | Optional deployment |

---

## Project Structure

```
Tron_X/
├── src/
│   ├── main.py              # FastAPI app entry point
│   ├── core/                # Settings, logging, exceptions, auth, rate limiting
│   ├── intelligence/        # Router, orchestrator, intent classifier, personas, CoT
│   ├── api/                 # REST endpoints (chat, memory, voice, system, iot, agents, ...)
│   ├── agents/               # Research, browser, calendar, email, scheduler, code, vision, CAD
│   ├── memory/               # ChromaDB, embeddings, RAG pipeline, episodic memory
│   ├── system/                # OS control, PowerShell runner, file ops, sandboxed executor
│   ├── voice/                 # STT/TTS/VAD/wake word
│   ├── vision/                # Screen capture, OCR, vision LLM
│   ├── iot/                   # Home Assistant + MQTT integration
│   ├── feeds/                 # Weather, stocks, news, crypto
│   ├── analytics/             # Usage tracking
│   └── plugins/               # Dynamic plugin system
├── config/                    # Models, personas, settings
├── static/                    # Three.js HUD frontend
├── deploy/                     # systemd service
├── Dockerfile / docker-compose.yml
└── requirements.txt
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full request-flow walkthrough, key design patterns, and a phase-by-phase breakdown of how the system was built.

---

## Getting Started

```bash
# 1. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 2. Configure
cp .env.example .env
# Add your API keys (at minimum GROQ_API_KEY for fast chat/voice)

# 3. Run
python run.py --reload

# 4. Open the HUD
# http://127.0.0.1:8000  → redirects to the Three.js frontend
# API docs: http://127.0.0.1:8000/docs
```

---

## API Surface

| Prefix | What it controls |
|---|---|
| `/api/chat` | Chat with LLM (streaming + non-streaming) |
| `/api/memory` | Document ingestion, semantic search, RAG |
| `/api/memory/episodic` | Episodic memory: remember/recall/summarize sessions |
| `/api/voice` | Speech-to-text, text-to-speech, streaming TTS |
| `/api/system` | Files, browser, screen, code execution, PowerShell, processes |
| `/api/iot` | Home Assistant devices, MQTT, scenes, automations |
| `/api/agents` | Research, multi-agent pipelines, scheduler |
| `/api/calendar` | Google Calendar events + reminders |
| `/api/email` | IMAP fetch/search/summarize + SMTP send |
| `/api/whatsapp` | WhatsApp send/receive |
| `/api/feeds` | Weather, stocks, news, crypto |
| `/api/analytics` | Usage stats, latency charts |
| `/api/plugins` | Load/list dynamic plugins |
| `/api/health` | Health check, provider status, latency stats |

---

## License

This project is for personal/educational use.
