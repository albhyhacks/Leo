# Phase 0 — Preflight and start downloads

**Plan source:** `/home/kids/Tau/Tau/01-phase0-preflight.md`
**Status:** IN PROGRESS (pulls running)
**Started:** 2026-11-05 ~19:26
**Goal:** Confirm the Jetson is ready and start every large download early.

## Checklist

- [x] 1. Report system (L4T, free -h, df -h, nvpmodel)
- [x] 2. Set max performance (`nvpmodel -m 2`, `jetson_clocks`)
- [x] 3. Check desktop / RAM overhead
- [x] 4. Confirm Docker `nvidia` runtime
- [x] 5. Create `tau-orin/.env` from `baby-tau/.env_example`
- [x] 6. Start three image pulls in background
- [ ] 7. Final status: pulls finished, disk left, manual steps

## Evidence

### 1. System
```
L4T: R36 (release), REVISION: 5.0, BOARD: generic, EABI: aarch64
      DATE: Fri Jan 16 03:50:45 UTC 2026
OS:  Ubuntu 22.04.5 LTS (Jammy)
Kernel arch: aarch64
CUDA: 12.6 (V12.6.68)
RAM: 7.4Gi total, 3.0Gi used, 1.4Gi free, 4.4Gi available, 11Gi swap
Disk: /dev/nvme0n1p1 227G, 46G used, 170G avail (22%)
nvpmodel (before): NV Power Mode: 15W, mode 0
```

**Deviation:** plan expected L4T R36.4.x (JetPack 6.2). Machine is L4T **R36.5.0**
(and `nvidia-l4t-core 36.5.0-20260115194252`). CUDA 12.6 matches the r36.4.0
images. Upstream `dustynv` has no r36.5 tags (checked Docker Hub API for
`dustynv/ollama`, `dustynv/speaches`, `dustynv/kokoro-tts`), so the project's
r36.4.0 tags are the closest published build. Runtime test required in Phase 1.

### 2. Power mode
```
< POWER_MODEL ID=0 NAME=15W >
< POWER_MODEL ID=1 NAME=25W >
< POWER_MODEL ID=2 NAME=MAXN_SUPER >
After: NV Power Mode: MAXN_SUPER (2)
jetson_clocks: ran successfully (no output)
```

### 3. Desktop / RAM
```
systemctl get-default → graphical.target
```
Desktop is running and costs ~1.5 GB per `baby-tau/README.md`. User explicitly
chose **no reboot**, so we keep `graphical.target` and monitor RAM instead.
Fallbacks if tight: `llama3.2:1b`, `Systran/faster-whisper-base`.

### 4. Docker + GPU runtime
```
Docker version 29.4.1
Runtimes: runc io.containerd.runc.v2 nvidia
Default Runtime: runc
```
`nvidia` runtime is present. Pulls/services explicitly set `runtime: nvidia`.

### 5. `.env`
Created `tau-orin/.env` with:
- `OLLAMA_MODEL=llama3.2:3b`
- `SPEACHES_HOST_PORT=8001`, `VLLM_PORT=8000`, `KOKORO_TTS_HOST_PORT=8880`
- `SPEACHES_TAG=dustynv/speaches:r36.4.0-cu128-24.04`
- `KOKORO_TTS_TAG=dustynv/kokoro-tts:fastapi-r36.4.0-cu128-24.04`
- `OLLAMA_TAG=dustynv/ollama:r36.4.0`
- `STT_MODEL=Systran/faster-whisper-small` (verify in Phase 1)

### 6. Downloads (background)
```
logs/pull-ollama.log   → docker pull dustynv/ollama:r36.4.0
logs/pull-speaches.log → docker pull dustynv/speaches:r36.4.0-cu128-24.04
logs/pull-kokoro.log   → docker pull dustynv/kokoro-tts:fastapi-r36.4.0-cu128-24.04
```
Models (`llama3.2:3b`, `llama3.2:1b`) are pulled by Ollama in Phase 1 via
`docker exec tau-ollama ollama pull ...`.

### 7. Final status
All three images pulled successfully:
```
dustynv/ollama:r36.4.0                            15.3 GB (uncompressed)
dustynv/speaches:r36.4.0-cu128-24.04             21.3 GB
dustynv/kokoro-tts:fastapi-r36.4.0-cu128-24.04   17.2 GB
```
Docker GPU test (`docker run --rm --runtime=nvidia dustynv/ollama:r36.4.0 nvidia-smi`):
```
NVIDIA-SMI 540.5.0   Driver 540.5.0   CUDA 12.6
GPU: Orin (nvgpu)
```
Disk after pulls: 227 G total, 87 G used, **129 G free**.
RAM after pulls: 7.4 GiB total, 3.0 GiB used, 4.2 GiB available.
Actual runtime findings for Phase 1:
- Ollama image cmd is `/bin/sh -c "/start_ollama && /bin/bash"`; default
  `OLLAMA_HOST=0.0.0.0`, `OLLAMA_MODELS=/data/models/ollama/models`.
  Persist models by mounting `./ollama:/data/models/ollama/models`.
- Speaches entrypoint is `/opt/speaches/start-server.sh`, exposes 8000; set
  `PORT=8001`. Do **not** copy the `tail -f /dev/null` override.
- Kokoro entrypoint is `/opt/kokoro-fastapi/start-gpu.sh`, exposes 8880.

## Done-when check
- [x] `nvidia` runtime confirmed
- [x] power mode MAXN_SUPER
- [x] `tau-orin/.env` exists
- [x] all three pulls running or finished

**Phase 0: PASSED** (2026-11-05 ~20:30 local).
