FROM tau-bridge:local
RUN pip install --no-cache-dir sherpa-onnx python-multipart
WORKDIR /app
COPY server.py /app/server.py
CMD ["python", "/app/server.py"]
