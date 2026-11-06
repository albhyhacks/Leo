"""WebSocket /v1/realtime endpoint + event dispatch."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import struct
import time
import wave
from pathlib import Path

from starlette.websockets import WebSocket, WebSocketDisconnect

from . import dashboard, events, latency, memory
from .audio import decode_audio_appendix, resample_pcm16
from .barge_in import BargeInEvaluator
from .history_store import append_turn, build_system_prompt, correct_last_user_turn, time_prefix
from .llm_client import stream_sentences
from .protocol import CLIENT_SAMPLE_RATE, TTS_SAMPLE_RATE, gen_content_part_id, gen_item_id, gen_response_id
from .session import Session
from .stt_client import transcribe
from .tts_client import synthesize, trailing_pause_ms

log = logging.getLogger(__name__)
_AUDIO_DIR = Path("/logs/audio")


def _save_turn_audio(pcm_data: bytes) -> None:
    """Save a captured 24 kHz mono PCM16 turn and log its signal levels."""
    if not pcm_data:
        return
    samples = struct.unpack(f"<{len(pcm_data) // 2}h", pcm_data[: len(pcm_data) // 2 * 2])
    peak = max((abs(sample) for sample in samples), default=0)
    rms = (sum(sample * sample for sample in samples) / max(1, len(samples))) ** 0.5
    clipped = sum(abs(sample) >= 32760 for sample in samples)
    try:
        _AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        existing = [int(p.stem[5:]) for p in _AUDIO_DIR.glob("turn-*.wav")
                    if p.stem[5:].isdigit()]
        path = _AUDIO_DIR / f"turn-{max(existing, default=0) + 1}.wav"
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(pcm_data[: len(pcm_data) // 2 * 2])
        log.info("[AUDIO] saved %s duration=%.2fs peak=%d rms=%.1f clipped=%d",
                 path.name, len(samples) / 24000, peak, rms, clipped)
    except OSError:
        log.exception("[AUDIO] unable to save captured turn")

# The time-aware prefix (history_store.time_prefix) is meant for the model only.
# Small models sometimes repeat it, and it must never be spoken or persisted.
_TIME_ECHO_RE = re.compile(r"^\s*(?:\[\d{4}-\d{2}-\d{2}[^\]]*\]\s*)+")
_CORRECTION_RE = re.compile(
    r"^\s*(?:no[, ]+)?(?:i said|i meant|i actually said)\s+(.+?)\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def _strip_time_echo(text: str) -> str:
    """Drop a leading ``[YYYY-MM-DD HH:MM:SS][gap]`` prefix echoed by the model."""
    return _TIME_ECHO_RE.sub("", text, count=1).lstrip()


def _correction_text(transcript: str) -> str | None:
    """Extract only explicit ``No, I said …`` style corrections."""
    match = _CORRECTION_RE.match(transcript)
    return match.group(1).strip() if match else None


def _schedule_memory_extraction(session: Session) -> None:
    """Learn durable facts in the background (Phase 4).

    Called only after the reply has finished playing, so extraction never
    delays speech. Skipped while a previous extraction is still running.
    """
    task = session._memory_task
    if task is not None and not task.done():
        log.info("[MEMORY] previous extraction still running, skipping this turn")
        return
    session._memory_task = asyncio.create_task(memory.extract_and_save())
    log.info("[MEMORY] extraction task scheduled")


def _cancel_memory_extraction(session: Session) -> None:
    """Drop an in-flight extraction: the user is speaking, Tau needs the model."""
    task = session._memory_task
    if task is not None and not task.done():
        log.info("[MEMORY] cancelling extraction — user started speaking")
        task.cancel()


async def handle_realtime_ws(
    ws: WebSocket, model: str | None = None, presence: bool = False,
):
    """Main handler for a single Realtime API WebSocket connection."""
    await ws.accept(subprotocol="realtime")

    session = Session()
    session.init_vad()
    log.info("Session %s created", session.id)

    # Send session.created
    await ws.send_json(events.session_created(session.to_dict()))

    # Start sender task
    sender_task = asyncio.create_task(_sender_loop(ws, session))
    greeting_task = (
        asyncio.create_task(_send_connect_greeting(session)) if presence else None
    )

    try:
        await _receiver_loop(ws, session)
    except WebSocketDisconnect:
        log.info("Session %s disconnected", session.id)
    except Exception as e:
        log.error("Session %s error: %s", session.id, e)
    finally:
        session.cancel_event.set()
        sender_task.cancel()
        if greeting_task:
            greeting_task.cancel()
        if session._response_task and not session._response_task.done():
            session._response_task.cancel()
        if session._memory_task and not session._memory_task.done():
            session._memory_task.cancel()
        log.info("Session %s closed", session.id)


async def _send_connect_greeting(session: Session) -> None:
    """Speak one local, time-aware greeting when a new session connects."""
    session._pipeline_pending = True
    try:
        presence = dashboard.presence()
        name = presence.get("user_name")
        gap = presence.get("gap_seconds")
        if gap is not None and gap >= 4 * 60 * 60:
            greeting = f"Welcome back{f', {name}' if name else ''}."
        else:
            greeting = f"Good {presence['time_of_day']}{f', {name}' if name else ''}."
        pcm = await synthesize(greeting, voice=session.config.voice)
    except Exception:
        log.exception("Session %s connect greeting synthesis failed", session.id)
        session._pipeline_pending = False
        return
    if not pcm:
        log.warning("Session %s connect greeting returned no audio", session.id)
        session._pipeline_pending = False
        return

    response_id = gen_response_id()
    item_id = gen_item_id()
    response_obj = {
        "id": response_id, "object": "realtime.response", "status": "in_progress", "output": []
    }
    item = {
        "id": item_id, "object": "realtime.item", "type": "message", "role": "assistant",
        "content": [],
    }
    await session.send(events.response_created(response_obj))
    await session.send(events.response_output_item_added(response_id, 0, item))
    await session.send(events.response_content_part_added(
        response_id, item_id, 0, 0, {"type": "audio", "audio": ""},
    ))
    await session.send(events.response_content_part_added(
        response_id, item_id, 0, 1, {"type": "audio_transcript", "transcript": ""},
    ))
    await session.send(events.response_audio_transcript_delta(
        response_id, item_id, 0, 1, greeting,
    ))
    session.is_speaking = True
    try:
        for offset in range(0, len(pcm), 8192):
            chunk = pcm[offset:offset + 8192]
            await session.send(events.response_audio_delta(
                response_id, item_id, 0, 0, base64.b64encode(chunk).decode("ascii"),
            ))
        await session.send(events.response_audio_done(response_id, item_id, 0, 0))
        await session.send(events.response_audio_transcript_done(
            response_id, item_id, 0, 1, greeting,
        ))
        await session.send(events.response_content_part_done(
            response_id, item_id, 0, 0, {"type": "audio", "audio": ""},
        ))
        await session.send(events.response_content_part_done(
            response_id, item_id, 0, 1, {"type": "audio_transcript", "transcript": greeting},
        ))
        item["content"] = [
            {"type": "audio", "audio": ""},
            {"type": "audio_transcript", "transcript": greeting},
        ]
        await session.send(events.response_output_item_done(response_id, 0, item))
        response_obj.update(status="completed", output=[item])
        await session.send(events.response_done(response_obj))
        session.conversation.add_assistant_message(greeting, item_id=item_id)
        append_turn("assistant", greeting)
        log.info("Session %s greeting: %s", session.id, greeting)
        # Keep the non-AEC capture guard active through expected speaker output.
        await asyncio.sleep(len(pcm) / 2 / TTS_SAMPLE_RATE + 0.8)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Session %s connect greeting send failed", session.id)
    finally:
        session._pipeline_pending = False
        session.is_speaking = False
        if session.vad and not _is_aec_mode(session):
            session.vad.reset()


async def _sender_loop(ws: WebSocket, session: Session):
    """Drain the session send queue and forward events to the WebSocket."""
    try:
        while True:
            event = await session.send_queue.get()
            try:
                await ws.send_json(event)
            except Exception:
                break
    except asyncio.CancelledError:
        pass


async def _receiver_loop(ws: WebSocket, session: Session):
    """Receive client events and dispatch."""
    while True:
        data = await ws.receive_json()
        event_type = data.get("type", "")

        try:
            if event_type == "session.update":
                await _handle_session_update(session, data)
            elif event_type == "input_audio_buffer.append":
                await _handle_audio_append(session, data)
            elif event_type == "input_audio_buffer.commit":
                await _handle_audio_commit(session)
            elif event_type == "input_audio_buffer.clear":
                await _handle_audio_clear(session)
            elif event_type == "response.create":
                await _handle_response_create(session, data)
            elif event_type == "response.cancel":
                await _handle_response_cancel(session)
            else:
                await session.send(
                    events.error_event(f"Unknown event type: {event_type}", code="unknown_event")
                )
        except Exception as e:
            log.error("Error handling %s: %s", event_type, e, exc_info=True)
            await session.send(events.error_event(str(e)))


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------
async def _handle_session_update(session: Session, data: dict):
    session_data = data.get("session", {})
    session.config.update_from_dict(session_data)
    session.init_vad()
    await session.send(events.session_updated(session.to_dict()))
    log.info("Session %s updated", session.id)


async def _handle_audio_append(session: Session, data: dict):
    audio_b64 = data.get("audio", "")
    if not audio_b64:
        return

    # Append to buffer
    session.audio_buffer.append(audio_b64)

    # Feed to VAD if enabled
    vad = session.vad
    if vad is None:
        return

    pcm_bytes = decode_audio_appendix(audio_b64)

    # Update VAD speaking state
    vad.is_speaking = session.is_speaking

    # Pipeline guard (non-AEC): suppress VAD while pipeline is pending or running
    # to prevent spurious speech_stopped → auto_commit → pipeline restart
    if not _is_aec_mode(session) and _pipeline_active(session):
        return

    vad_events = vad.process_chunk(pcm_bytes)
    for vad_event in vad_events:
        from .vad import VADEventType

        if vad_event.type == VADEventType.SPEECH_STARTED:
            log.info("[VAD] speech_started at %dms", vad_event.audio_ms)
            await session.send(events.input_audio_buffer_speech_started(vad_event.audio_ms))
            _cancel_memory_extraction(session)

            # If assistant is speaking and AEC mode, start barge-in evaluation
            if session.is_speaking and _is_aec_mode(session):
                _start_barge_in_evaluation(session, vad_event.audio_bytes)

        elif vad_event.type == VADEventType.SPEECH_STOPPED:
            log.info("[VAD] speech_stopped at %dms (audio=%d bytes, is_speaking=%s, aec=%s)",
                     vad_event.audio_ms, len(vad_event.audio_bytes),
                     session.is_speaking, _is_aec_mode(session))
            await session.send(events.input_audio_buffer_speech_stopped(vad_event.audio_ms))

            if session.is_speaking and _is_aec_mode(session):
                # Barge-in: evaluate the accumulated audio
                asyncio.create_task(
                    _evaluate_barge_in(session, vad_event.audio_bytes)
                )
            else:
                # Normal turn end — auto-commit with the VAD-captured audio
                await _auto_commit(session, vad_event.audio_bytes)


def _is_aec_mode(session: Session) -> bool:
    td = session.config.turn_detection
    return td is not None and td.aec_mode == "aec"


def _pipeline_active(session: Session) -> bool:
    """True if a pipeline is pending (scheduled but not started) or running."""
    if session._pipeline_pending:
        return True
    if session._response_task and not session._response_task.done():
        return True
    return False


def _start_barge_in_evaluation(session: Session, initial_audio: bytes):
    """Initialize barge-in audio collection (the VAD already accumulated speech)."""
    # The VAD speech_started event already includes pre-roll audio,
    # but the main evaluation happens on speech_stopped with all accumulated audio
    log.info("Barge-in: speech detected during response playback, waiting for turn end...")


async def _evaluate_barge_in(session: Session, audio_bytes: bytes):
    """Run intelligent barge-in evaluation on accumulated speech audio."""
    evaluator = BargeInEvaluator()
    decision = await evaluator.evaluate(
        audio_pcm16_24khz=audio_bytes,
        assistant_last_text=session.current_response_text,
    )

    if decision == "STOP":
        log.info("Barge-in: STOP — cancelling response and starting new turn")
        await _handle_response_cancel(session)
        # Start new turn with the captured audio
        await _run_pipeline(session, audio_bytes)
    else:
        log.info("Barge-in: CONTINUE — resuming playback")
        # Discard the snippet, keep playing


async def _handle_audio_commit(session: Session):
    if session.audio_buffer.is_empty:
        return

    pcm_data = session.audio_buffer.commit()
    item_id = gen_item_id()
    await session.send(events.input_audio_buffer_committed(item_id))

    # Run the STT → LLM → TTS pipeline
    asyncio.create_task(_run_pipeline(session, pcm_data, item_id=item_id))


async def _auto_commit(session: Session, vad_audio: bytes):
    """Auto-commit from VAD speech_stopped with the VAD-accumulated audio."""
    if _pipeline_active(session):
        log.info("[GUARD] auto_commit suppressed — pipeline pending=%s task=%s",
                 session._pipeline_pending,
                 "running" if session._response_task and not session._response_task.done() else "none/done")
        return

    log.info("[PIPELINE] auto_commit → starting pipeline (audio=%d bytes)", len(vad_audio))
    # Set pending flag BEFORE creating the task to close the race window
    session._pipeline_pending = True

    item_id = gen_item_id()
    await session.send(events.input_audio_buffer_committed(item_id))
    session.audio_buffer.clear()  # Clear the main buffer since we use VAD audio

    asyncio.create_task(_run_pipeline(session, vad_audio, item_id=item_id))


async def _handle_audio_clear(session: Session):
    session.audio_buffer.clear()
    if session.vad:
        session.vad.reset()
    await session.send(events.input_audio_buffer_cleared())


async def _handle_response_create(session: Session, data: dict):
    """Handle explicit response.create — may include text input for text-based generation."""
    resp_data = data.get("response", {})
    input_items = resp_data.get("input", [])

    # If input items provided, add them to conversation
    for item in input_items:
        if item.get("type") == "message" and item.get("role") == "user":
            for content in item.get("content", []):
                if content.get("type") == "input_text":
                    text = content["text"]
                    session.conversation.add_user_message(f"{time_prefix()} {text}")
                    append_turn("user", text)

    # Run LLM → TTS pipeline without STT
    asyncio.create_task(_run_response_pipeline(session))


async def _handle_response_cancel(session: Session):
    log.info("[CANCEL] response cancel requested (task=%s, pending=%s)",
             "running" if session._response_task and not session._response_task.done() else "none/done",
             session._pipeline_pending)
    session.cancel_event.set()
    session._pipeline_pending = False
    if session._response_task and not session._response_task.done():
        session._response_task.cancel()
    session.is_speaking = False


# ---------------------------------------------------------------------------
# Pipeline: STT → LLM → TTS
# ---------------------------------------------------------------------------
async def _run_pipeline(session: Session, pcm_data: bytes, item_id: str | None = None):
    """Full pipeline: transcribe audio, run LLM, stream TTS back."""
    # Cancel any existing response
    if session._response_task and not session._response_task.done():
        log.info("[PIPELINE] cancelling existing pipeline before starting new one")
        session.cancel_event.set()
        session._response_task.cancel()
        try:
            await session._response_task
        except (asyncio.CancelledError, Exception):
            pass

    session.cancel_event.clear()
    item_id = item_id or gen_item_id()

    async def _pipeline():
        await asyncio.to_thread(_save_turn_audio, pcm_data)
        # Phase 5: stamp end-of-speech as soon as the pipeline runs; the
        # task starts within a few milliseconds of the VAD commit.
        session.start_turn()
        try:
            # 1. STT: transcribe
            log.info("[PIPELINE] STT starting (audio=%d bytes)", len(pcm_data))
            transcript = await transcribe(pcm_data)
            session.mark("transcript")
            if not transcript:
                log.warning("[PIPELINE] empty transcription, skipping response")
                return

            correction = _correction_text(transcript)
            if correction and correct_last_user_turn(correction):
                prefixed_correction = f"{time_prefix()} {correction}"
                session.conversation.correct_latest_user_message(prefixed_correction)
                await session.send(events.input_audio_transcription_completed(
                    item_id, 0, correction,
                ))
                log.info("[PIPELINE] corrected the previous user turn: %s", correction)
                await _generate_response(session)
                return

            # Emit transcription event
            await session.send(
                events.input_audio_transcription_completed(item_id, 0, transcript)
            )

            # Add user message to the LLM conversation, with time awareness.
            # The prefix stays out of the browser transcript and history file.
            prefixed = f"{time_prefix()} {transcript}"
            session.conversation.add_user_message(prefixed, item_id=item_id)
            append_turn("user", transcript)
            log.info("[PIPELINE] STT done → User: %s", transcript)

            # 2. LLM → TTS
            await _generate_response(session)
        finally:
            session._pipeline_pending = False

    session._response_task = asyncio.create_task(_pipeline())
    # Clear pending flag now that the task is created and tracked
    # (the task itself also clears in finally, for safety on early return)
    log.info("[PIPELINE] task created (item=%s)", item_id)


async def _run_response_pipeline(session: Session):
    """Pipeline without STT — for text-based or re-generate requests."""
    if session._response_task and not session._response_task.done():
        session.cancel_event.set()
        session._response_task.cancel()
        try:
            await session._response_task
        except (asyncio.CancelledError, Exception):
            pass

    session.cancel_event.clear()
    # Defensive: a typed turn must not inherit the timing marks of the voice
    # turn before it (latency.record_turn checks for a speech_end too).
    session.timing.clear()
    session._response_task = asyncio.create_task(_generate_response(session))


async def _generate_response(session: Session):
    """Run LLM streaming → sentence splitting → TTS streaming → audio events."""
    response_id = gen_response_id()
    output_item_id = gen_item_id()
    audio_content_id = gen_content_part_id()
    transcript_content_id = gen_content_part_id()

    # Response created
    response_obj = {
        "id": response_id,
        "object": "realtime.response",
        "status": "in_progress",
        "output": [],
    }
    await session.send(events.response_created(response_obj))

    # Output item
    output_item = {
        "id": output_item_id,
        "object": "realtime.item",
        "type": "message",
        "role": "assistant",
        "content": [],
    }
    await session.send(events.response_output_item_added(response_id, 0, output_item))

    # Content parts — audio and transcript
    has_audio = "audio" in session.config.modalities
    if has_audio:
        await session.send(
            events.response_content_part_added(
                response_id, output_item_id, 0, 0,
                {"type": "audio", "audio": ""},
            )
        )
        await session.send(
            events.response_content_part_added(
                response_id, output_item_id, 0, 1,
                {"type": "audio_transcript", "transcript": ""},
            )
        )

    session.is_speaking = True
    session.current_response_text = ""
    full_transcript = ""
    cancelled = False
    sentence_count = 0
    tts_chunk_count = 0
    total_tts_bytes_raw = 0      # raw PCM16 from TTS (TTS_SAMPLE_RATE)
    total_tts_bytes_resampled = 0  # resampled PCM16 sent to client (24000Hz)
    first_audio_send_t: float | None = None
    last_audio_send_t: float | None = None

    # Ollama returns one final chunk with token usage when
    # stream_options.include_usage is set. Keep the last dict we see.
    usage_holder: dict = {}

    log.info("[RESPONSE] %s starting (has_audio=%s)", response_id, has_audio)

    try:
        # Browser instructions win; otherwise Tau's prompt, re-read each turn
        # so edits to tau.system.md / facts.md apply without a restart.
        system_prompt = build_system_prompt(session.config.instructions)
        messages = session.conversation.to_chat_messages(system_prompt)

        tts_mode = session.config.tts_mode  # "whole" or "sentence"
        log.info("[RESPONSE] tts_mode=%s", tts_mode)

        if tts_mode == "whole":
            # ── Whole-response mode: accumulate full text, single TTS call ──
            full_text_for_tts = ""
            async for sentence in stream_sentences(
                messages,
                temperature=session.config.temperature,
                cancel_event=session.cancel_event,
                first_token_cb=lambda: session.mark("first_token"),
                usage_cb=usage_holder.update,
            ):
                if session.cancel_event.is_set():
                    cancelled = True
                    break

                sentence_count += 1
                sentence = _strip_time_echo(sentence)
                full_text_for_tts += sentence + " "
                full_transcript = full_text_for_tts
                session.current_response_text = full_transcript
                log.info("[RESPONSE] sentence #%d: %s", sentence_count, sentence[:80])

                if has_audio:
                    await session.send(
                        events.response_audio_transcript_delta(
                            response_id, output_item_id, 0, 1, sentence + " ",
                        )
                    )

            session.mark("llm_done")

            # Single TTS call for the entire response
            if has_audio and full_text_for_tts.strip() and not cancelled and not session.cancel_event.is_set():
                tts_pcm = await synthesize(
                    full_text_for_tts.strip(),
                    voice=session.config.voice,
                    cancel_event=session.cancel_event,
                )

                # Re-check cancel after the (potentially long) TTS call
                if session.cancel_event.is_set():
                    cancelled = True

                if tts_pcm and not cancelled:
                    total_tts_bytes_raw += len(tts_pcm)
                    resampled = resample_pcm16(tts_pcm, TTS_SAMPLE_RATE, CLIENT_SAMPLE_RATE)
                    total_tts_bytes_resampled += len(resampled)

                    chunk_bytes = CLIENT_SAMPLE_RATE // 10 * 2  # 100ms of PCM16
                    for offset in range(0, len(resampled), chunk_bytes):
                        if session.cancel_event.is_set():
                            cancelled = True
                            break
                        chunk = resampled[offset:offset + chunk_bytes]
                        chunk_b64 = base64.b64encode(chunk).decode("ascii")
                        tts_chunk_count += 1
                        now = time.monotonic()
                        if first_audio_send_t is None:
                            first_audio_send_t = now
                        last_audio_send_t = now
                        await session.send(
                            events.response_audio_delta(
                                response_id, output_item_id, 0, 0, chunk_b64,
                            )
                        )
                elif not tts_pcm and not cancelled:
                    log.error("[RESPONSE] TTS returned empty for whole-mode text (%d chars): %s",
                              len(full_text_for_tts), full_text_for_tts[:120])

        else:
            # ── Sentence-by-sentence: pipelined LLM → TTS ──
            # Producer drains the LLM stream into a queue while the
            # consumer synthesises and sends audio concurrently.  This
            # keeps the LLM connection alive during long TTS calls and
            # lets later sentences queue up while earlier ones play.
            _MIN_TTS_CHARS = 20
            sentence_queue: asyncio.Queue[str | None] = asyncio.Queue()

            async def _llm_producer():
                nonlocal full_transcript, sentence_count, cancelled
                try:
                    async for sentence in stream_sentences(
                        messages,
                        temperature=session.config.temperature,
                        cancel_event=session.cancel_event,
                        first_token_cb=lambda: session.mark("first_token"),
                        usage_cb=usage_holder.update,
                    ):
                        if session.cancel_event.is_set():
                            cancelled = True
                            break
                        sentence_count += 1
                        sentence = _strip_time_echo(sentence)
                        full_transcript += sentence + " "
                        session.current_response_text = full_transcript
                        log.info("[RESPONSE] sentence #%d: %s", sentence_count, sentence[:80])

                        if has_audio:
                            await session.send(
                                events.response_audio_transcript_delta(
                                    response_id, output_item_id, 0, 1, sentence + " ",
                                )
                            )
                        await sentence_queue.put(sentence)
                        log.debug("[RESPONSE] producer queued #%d (%d chars, qsize=%d)",
                                  sentence_count, len(sentence), sentence_queue.qsize())
                finally:
                    session.mark("llm_done")
                    await sentence_queue.put(None)  # sentinel
                    log.info("[RESPONSE] producer done — %d sentences queued", sentence_count)

            async def _tts_consumer():
                nonlocal tts_chunk_count, total_tts_bytes_raw, total_tts_bytes_resampled
                nonlocal first_audio_send_t, last_audio_send_t, cancelled
                tts_buffer = ""

                consumer_idx = 0
                while True:
                    sentence = await sentence_queue.get()
                    if sentence is None:
                        log.info("[RESPONSE] consumer: sentinel received, done")
                        break
                    if session.cancel_event.is_set():
                        log.info("[RESPONSE] consumer: cancelled, discarding queued sentences")
                        cancelled = True
                        break
                    if not has_audio:
                        continue

                    consumer_idx += 1
                    tts_buffer = (tts_buffer + " " + sentence).strip() if tts_buffer else sentence

                    # Keep buffering until we have enough text for a good TTS call
                    if len(tts_buffer) < _MIN_TTS_CHARS:
                        log.debug("[RESPONSE] buffering short sentence (%d chars): %s",
                                  len(tts_buffer), tts_buffer[:60])
                        continue

                    log.info("[RESPONSE] consumer: TTS chunk #%d (%d chars, qsize=%d): %s",
                             consumer_idx, len(tts_buffer), sentence_queue.qsize(),
                             tts_buffer[:80])
                    tts_pcm = await synthesize(
                        tts_buffer,
                        voice=session.config.voice,
                        cancel_event=session.cancel_event,
                    )
                    tts_text_for_pause = tts_buffer
                    tts_buffer = ""
                    if not tts_pcm:
                        log.warning("[RESPONSE] consumer: TTS returned EMPTY for chunk #%d: %s",
                                    consumer_idx, tts_text_for_pause[:80])
                        continue

                    audio_dur = len(tts_pcm) / 2 / TTS_SAMPLE_RATE
                    log.info("[RESPONSE] consumer: chunk #%d → %d bytes (%.2fs audio): %s",
                             consumer_idx, len(tts_pcm), audio_dur, tts_text_for_pause[:80])

                    # Append inter-sentence silence based on trailing punctuation
                    pause_ms = trailing_pause_ms(tts_text_for_pause)
                    if pause_ms > 0:
                        silence_samples = int(TTS_SAMPLE_RATE * pause_ms / 1000)
                        tts_pcm += b'\x00' * (silence_samples * 2)
                        log.debug("[RESPONSE] appended %dms silence for: %s",
                                  pause_ms, tts_text_for_pause[-20:])

                    total_tts_bytes_raw += len(tts_pcm)

                    resampled = resample_pcm16(tts_pcm, TTS_SAMPLE_RATE, CLIENT_SAMPLE_RATE)
                    total_tts_bytes_resampled += len(resampled)

                    chunk_bytes = CLIENT_SAMPLE_RATE // 10 * 2  # 100ms of PCM16
                    for offset in range(0, len(resampled), chunk_bytes):
                        chunk = resampled[offset:offset + chunk_bytes]
                        chunk_b64 = base64.b64encode(chunk).decode("ascii")
                        tts_chunk_count += 1
                        now = time.monotonic()
                        if first_audio_send_t is None:
                            first_audio_send_t = now
                        last_audio_send_t = now
                        await session.send(
                            events.response_audio_delta(
                                response_id, output_item_id, 0, 0, chunk_b64,
                            )
                        )

                    if session.cancel_event.is_set():
                        cancelled = True
                        break

                # Flush remaining buffered text
                if tts_buffer and has_audio and not cancelled:
                    log.info("[RESPONSE] flushing TTS buffer (%d chars): %s",
                             len(tts_buffer), tts_buffer[:80])
                    tts_pcm = await synthesize(
                        tts_buffer,
                        voice=session.config.voice,
                        cancel_event=session.cancel_event,
                    )
                    if tts_pcm:
                        pause_ms = trailing_pause_ms(tts_buffer)
                        if pause_ms > 0:
                            silence_samples = int(TTS_SAMPLE_RATE * pause_ms / 1000)
                            tts_pcm += b'\x00' * (silence_samples * 2)
                        total_tts_bytes_raw += len(tts_pcm)
                        resampled = resample_pcm16(tts_pcm, TTS_SAMPLE_RATE, CLIENT_SAMPLE_RATE)
                        total_tts_bytes_resampled += len(resampled)
                        chunk_bytes = CLIENT_SAMPLE_RATE // 10 * 2
                        for offset in range(0, len(resampled), chunk_bytes):
                            chunk = resampled[offset:offset + chunk_bytes]
                            chunk_b64 = base64.b64encode(chunk).decode("ascii")
                            tts_chunk_count += 1
                            now = time.monotonic()
                            if first_audio_send_t is None:
                                first_audio_send_t = now
                            last_audio_send_t = now
                            await session.send(
                                events.response_audio_delta(
                                    response_id, output_item_id, 0, 0, chunk_b64,
                                )
                            )

            # Run LLM producer and TTS consumer concurrently
            producer = asyncio.create_task(_llm_producer())
            consumer = asyncio.create_task(_tts_consumer())
            try:
                await asyncio.gather(producer, consumer)
            finally:
                for task in (producer, consumer):
                    if not task.done():
                        task.cancel()

    except asyncio.CancelledError:
        cancelled = True
    except Exception as e:
        log.error("Response generation error: %s", e, exc_info=True)
        await session.send(events.error_event(str(e)))
        cancelled = True

    # Persist the assistant turn now; the echo cooldown below can last as long
    # as the spoken reply, and a reload during it must not lose the turn.
    if full_transcript.strip():
        append_turn("assistant", full_transcript.strip())

    # Phase 5: one latency row per turn, written before the echo cooldown so
    # the total is speech end → last audio chunk and not cooldown-inflated.
    # Phase 6: the same numbers go to the browser as tau.metrics.
    turn_metrics = latency.record_turn(
        session,
        first_audio_t=first_audio_send_t,
        last_audio_t=last_audio_send_t,
        sentences=sentence_count,
        reply_chars=len(full_transcript),
        status="cancelled" if cancelled else "completed",
        llm_tokens=usage_holder.get("completion_tokens"),
    )
    if turn_metrics:
        turn_metrics["turn_id"] = response_id
        try:
            await session.send(events.tau_metrics(turn_metrics))
        except Exception:  # noqa: BLE001 - telemetry must never break a turn
            log.debug("[LATENCY] could not send tau.metrics", exc_info=True)

    # --- Post-response echo cooldown (non-AEC only) ---
    if (
        has_audio
        and total_tts_bytes_raw > 0
        and not cancelled
        and session.vad is not None
        and not _is_aec_mode(session)
    ):
        total_audio_s = total_tts_bytes_raw / 2 / TTS_SAMPLE_RATE
        send_span_s = (
            (last_audio_send_t - first_audio_send_t)
            if first_audio_send_t is not None and last_audio_send_t is not None
            else 0.0
        )
        remaining_s = max(0.0, total_audio_s - send_span_s)
        cooldown_s = remaining_s + 0.8

        log.info("[ECHO_COOLDOWN] audio=%.2fs, send_span=%.2fs, cooldown=%.2fs",
                 total_audio_s, send_span_s, cooldown_s)
        try:
            await asyncio.wait_for(session.cancel_event.wait(), timeout=cooldown_s)
            log.info("[ECHO_COOLDOWN] interrupted by cancel_event")
            cancelled = True
        except asyncio.TimeoutError:
            log.info("[ECHO_COOLDOWN] cooldown complete")

    # Phase 4: the reply has been spoken, so it is safe to spend the model on
    # a background extraction now. Never awaited — memory must not add latency.
    _schedule_memory_extraction(session)

    # Finalize
    session.is_speaking = False
    full_transcript = full_transcript.strip()

    if full_transcript:
        session.conversation.add_assistant_message(full_transcript, item_id=output_item_id)

    # Emit done events
    if has_audio and not cancelled:
        await session.send(
            events.response_audio_done(response_id, output_item_id, 0, 0)
        )
        await session.send(
            events.response_audio_transcript_done(
                response_id, output_item_id, 0, 1, full_transcript,
            )
        )

    # Content part done
    if has_audio:
        await session.send(
            events.response_content_part_done(
                response_id, output_item_id, 0, 0,
                {"type": "audio", "audio": ""},
            )
        )
        await session.send(
            events.response_content_part_done(
                response_id, output_item_id, 0, 1,
                {"type": "audio_transcript", "transcript": full_transcript},
            )
        )

    # Output item done
    output_item["content"] = [
        {"type": "audio", "audio": ""},
        {"type": "audio_transcript", "transcript": full_transcript},
    ]
    await session.send(
        events.response_output_item_done(response_id, 0, output_item)
    )

    # Response done
    status = "cancelled" if cancelled else "completed"
    response_obj["status"] = status
    response_obj["output"] = [output_item]
    await session.send(events.response_done(response_obj))

    total_dur = total_tts_bytes_raw / 2 / TTS_SAMPLE_RATE if total_tts_bytes_raw else 0
    log.info("[RESPONSE] %s %s — %d sentences, %d tts chunks, %d chars, "
             "raw=%d bytes (%.2fs @%dHz), sent=%d bytes to client",
             response_id, status, sentence_count, tts_chunk_count, len(full_transcript),
             total_tts_bytes_raw, total_dur, TTS_SAMPLE_RATE, total_tts_bytes_resampled)

    # Reset VAD after response is done (for non-AEC echo cooldown)
    if session.vad and not _is_aec_mode(session):
        session.vad.reset()
