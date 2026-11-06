"""Pydantic Settings — configuration from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Service URLs (host networking: all services on localhost)
    tts_url: str = "http://localhost:8880"
    stt_url: str = "http://localhost:9002"

    # LLM backend (Ollama, OpenAI-compatible)
    openai_base_url: str = "http://localhost:8000"
    openai_api_key: str = "EMPTY"
    openai_model: str = "llama3.2:1b"

    # STT (Parakeet TDT 0.6B v2 int8 via sherpa-onnx, see ../parakeet)
    stt_model: str = "parakeet-tdt-0.6b-v2-int8"

    # TTS defaults (Kokoro)
    default_voice: str = "af_alloy"
    tts_speed: int = 100
    tts_concurrency: int = 1  # max parallel TTS requests (1 = serial)

    # VAD settings
    vad_threshold: float = 0.65
    vad_silence_ms: int = 600
    vad_prefix_padding_ms: int = 300

    # Reply cap: the 1B model ignores the 1-3 sentence prompt rule, so enforce it.
    max_reply_sentences: int = 3
    max_reply_tokens: int = 120

    # STT: drop transcripts shorter than this many letters (noise / breaths)
    stt_min_chars: int = 2

    # Turn detection
    default_turn_detection: str = "server_vad"
    default_aec_mode: str = "none"

    # Barge-in settings
    barge_in_window_ms: int = 750
    barge_in_model: str | None = None

    # Long-term memory (Phase 4). Extraction is rule-based over the user's own
    # sentences, so it cannot invent facts and needs no model call — safe to
    # leave on. See memory.py and phases/phase4.md for why the LLM was dropped.
    memory_enabled: bool = True
    memory_turns: int = 6        # history turns fed to each extraction
    memory_max_lines: int = 40   # oldest facts dropped beyond this

    # Per-turn latency log (Phase 5). The host directory ./logs is mounted
    # at /logs, so rows land in tau-orin/logs/latency.csv.
    latency_enabled: bool = True
    latency_csv: str = "/logs/latency.csv"

    # Server
    host: str = "0.0.0.0"
    port: int = 8080

    # HTTPS (browsers require a secure origin for getUserMedia)
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None


settings = Settings()
