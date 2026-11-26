FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
# httpx is only needed by the tests
RUN grep -v '^httpx' requirements.txt | sed 's/ *#.*//' > prod-requirements.txt && pip install -r prod-requirements.txt
COPY app ./app
COPY migrations ./migrations
RUN useradd -r -u 10001 spa
USER spa
ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8080/api/health',timeout=2)"
CMD ["python", "-m", "app"]
