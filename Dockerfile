# Production image: docker build -t parkpilot . && docker run -p 10000:10000 --env-file .env parkpilot
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Run as an unprivileged user.
RUN useradd --system --create-home parkpilot && chown -R parkpilot /srv/app
USER parkpilot

ENV APP_ENV=production PORT=10000
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','10000'))"
CMD ["bash", "start.sh"]
