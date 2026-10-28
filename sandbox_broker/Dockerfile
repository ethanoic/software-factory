FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY app.py .
# Runs as root only because the mounted Docker socket requires it; it has no egress network.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
