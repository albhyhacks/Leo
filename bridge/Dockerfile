FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY static/ static/
COPY src/ src/
# aarch64 PyPI torch wheels bundle CUDA (several GB). The bridge only runs
# Silero VAD on the CPU, so install the small CPU-only wheel explicitly.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch==2.10.0+cpu" \
    && pip install --no-cache-dir .

EXPOSE 8080
CMD ["python", "-m", "realtime_api.main"]
