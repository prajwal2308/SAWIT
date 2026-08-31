FROM python:3.12-slim

# ffmpeg does the audio conversion and the frame grabs; it is not optional.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
ENV SAWIT_DB=/data/sawit.sqlite3
# No VOLUME directive: it is only a hint here — Fly mounts through fly.toml's
# [mounts] and Railway rejects the instruction outright in favour of its own
# volumes. Both still land the database on a real disk at /data.
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
