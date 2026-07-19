FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

WORKDIR /app

COPY torrent-tidy.py /app/torrent-tidy.py
COPY config.py /app/config.py
COPY clients.py /app/clients.py
COPY state.py /app/state.py
COPY service.py /app/service.py

RUN adduser --disabled-password --gecos "" --home /nonexistent --shell /usr/sbin/nologin appuser \
  && chown appuser:appuser /app/torrent-tidy.py /app/config.py /app/clients.py /app/state.py /app/service.py

USER appuser

CMD ["python", "/app/torrent-tidy.py"]
