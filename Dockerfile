FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORTAL_DATA=/data \
    RAIL_DATA_DIR=/app/data

WORKDIR /app
COPY pyproject.toml ./
COPY mapmatch ./mapmatch
RUN pip install --no-cache-dir ".[rail]" && rm -rf build *.egg-info
# GB rail alignments and stations used to recognise and draw train journeys.
COPY data/rail_shapes.json.gz data/rail_stations.json.gz ./data/

RUN useradd --system --uid 10001 mapmatch && mkdir -p /data && chown mapmatch /data
USER mapmatch
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:8080' + os.environ.get('PORTAL_BASE_PATH','').rstrip('/') + '/healthz', timeout=4)"

CMD ["uvicorn", "mapmatch.portal.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
