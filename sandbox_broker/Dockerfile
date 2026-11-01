FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY app.py archive.py ./
# No Docker socket in here: the broker reaches Docker only through the docker-proxy service,
# which allows the container and volume calls it needs. So it runs unprivileged.
USER 10001:10001
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
