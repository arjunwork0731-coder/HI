FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 DATA_DIR=/app/data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# run as an unprivileged user: generated code executes in a sandboxed subprocess of this user
RUN useradd -m -u 1000 verimind && mkdir -p /app/data && chown -R verimind /app
USER verimind

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
