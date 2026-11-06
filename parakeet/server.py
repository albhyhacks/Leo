"""Parakeet-TDT 0.6B v2 (int8, sherpa-onnx) behind an OpenAI-style STT API.

POST /v1/audio/transcriptions  (multipart: file=<wav>, model, language) -> {"text": ...}
GET  /v1/health/ready
"""
import io
import logging
import os
import time
import wave

import numpy as np
import sherpa_onnx
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from scipy.signal import resample_poly

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("parakeet")

M = os.environ.get("PARAKEET_DIR", "/models")
THREADS = int(os.environ.get("PARAKEET_THREADS", "4"))
PORT = int(os.environ.get("PARAKEET_PORT", "9002"))

t0 = time.time()
recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
    encoder=f"{M}/encoder.int8.onnx",
    decoder=f"{M}/decoder.int8.onnx",
    joiner=f"{M}/joiner.int8.onnx",
    tokens=f"{M}/tokens.txt",
    model_type="nemo_transducer",
    num_threads=THREADS,
    provider="cpu",
    decoding_method="greedy_search",
)
log.info("Parakeet loaded in %.1fs (%d threads)", time.time() - t0, THREADS)

app = FastAPI()


def read_wav(data: bytes):
    with wave.open(io.BytesIO(data), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    assert sw == 2, "only PCM16 wav supported"
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        x = resample_poly(x, 16000, sr).astype(np.float32)
    return x


@app.get("/health")
@app.get("/v1/health/ready")
def ready():
    return {"status": "ok"}


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model: str = Form(""), language: str = Form("en")):
    x = read_wav(await file.read())
    if len(x) < 1600:
        return {"text": ""}
    # 0.4 s of silence each side helps transducers on tightly cropped clips
    pad = np.zeros(6400, dtype=np.float32)
    x = np.concatenate([pad, x, pad])
    t = time.time()
    s = recognizer.create_stream()
    s.accept_waveform(16000, x)
    recognizer.decode_stream(s)
    text = s.result.text.strip()
    log.info("%.2fs audio -> %.2fs: %s", len(x) / 16000, time.time() - t, text)
    return {"text": text}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
