# One image for the dashboard, the advisory planner and the report commands.
# See docker-compose.yml for how they are run together.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY wellopt ./wellopt
COPY config ./config
COPY assets ./assets
COPY reports ./reports
COPY app.py Well_Summarys.csv Final_Data_hourly.csv ./

# Run as an unprivileged user. var/ (plan store), models/ and reports/ are the only
# places the app writes; docker-compose.yml mounts volumes there.
RUN useradd --create-home --uid 10001 wellopt \
 && mkdir -p var models reports \
 && chown -R wellopt:wellopt var models reports
USER wellopt

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)"

# One process keeps a single SQLite reader; threads serve concurrent page loads.
CMD ["gunicorn", "app:server", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "120"]
