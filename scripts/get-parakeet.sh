#!/usr/bin/env bash
# Download the Parakeet TDT 0.6B v2 int8 model (sherpa-onnx format, ~230 MB)
# into parakeet/models/. Run once, with internet; Leo is offline afterwards.
set -euo pipefail
cd "$(dirname "$0")/../parakeet"
mkdir -p models && cd models
B=https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8/resolve/main
for f in tokens.txt decoder.int8.onnx joiner.int8.onnx encoder.int8.onnx; do
  [ -s "$f" ] && { echo "have $f"; continue; }
  echo "downloading $f"; curl -fL --retry 3 -C - -o "$f" "$B/$f"
done
echo "Parakeet model ready in $(pwd)"
