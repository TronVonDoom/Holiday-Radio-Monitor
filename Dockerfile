FROM python:3.12-slim

LABEL org.opencontainers.image.title="Holiday Radio Matcher" \
      org.opencontainers.image.description="Monitors holiday radio streams, matches songs against MusicBrainz and Spotify, and builds playlists." \
      org.opencontainers.image.source="https://github.com/TronVonDoom/Holiday-Radio-Monitor" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HRM_CONFIG_DIR=/config \
    HRM_PLAYLIST_DIR=/playlists \
    HRM_PORT=8080

WORKDIR /app

# Dependencies first so code edits do not invalidate the wheel layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /config /playlists

EXPOSE 8080

# UnRaid shows the container as unhealthy if the UI stops responding.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; port=os.environ.get('HRM_PORT','8080'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+port+'/healthz', timeout=8).status == 200 else 1)"

CMD ["python", "-m", "app.main"]
