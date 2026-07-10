FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

WORKDIR /app

COPY torrent-tidy.py /app/torrent-tidy.py

RUN adduser --disabled-password --gecos "" --home /nonexistent --shell /usr/sbin/nologin appuser \
  && chown appuser:appuser /app/torrent-tidy.py

USER appuser

CMD ["python", "/app/torrent-tidy.py"]
