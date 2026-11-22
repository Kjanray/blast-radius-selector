# Same base image the commits were labeled in (decides which sqlglot tests run).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
# Pristine sqlglot at the pinned commit, straight from its own repository.
RUN python -c "import urllib.request,tarfile,io; d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz').read(); tarfile.open(fileobj=io.BytesIO(d)).extractall('/opt', filter='tar')" \
 && mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot \
 && cd /opt/sqlglot && python -m compileall -q sqlglot tests > /dev/null; \
 chmod -R a+rX /opt/sqlglot
COPY selector.py runner_plugin.py map.json.gz /app/
RUN python -m compileall -q /app && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
