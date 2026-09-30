FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Mirror the local layout so main.py's Path(__file__).parent^3 / "frontend" resolves correctly.
# Local:  _trading/backend/app/main.py  →  _trading/frontend/
# Docker: /srv/backend/app/main.py      →  /srv/frontend/
WORKDIR /srv

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Headless browser for sites that only show their text after JavaScript
# runs (jstext.render): Debian's chromium, driven by Playwright. Lorenzo,
# 30/09: "Installa". SITE_RENDER=0 in .env switches it off.
RUN apt-get update && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "playwright>=1.47,<2"
ENV CHROMIUM_PATH=/usr/bin/chromium

COPY backend/app ./backend/app
COPY backend/app_entry.py ./backend/app_entry.py
COPY frontend ./frontend
COPY docker-entrypoint.sh ./docker-entrypoint.sh

# Run uvicorn from the backend sub-dir so `app.main` import resolves
# Il processo applicativo non gira da root: se un giorno qualcosa dentro
# l'app venisse sfruttato, chi entra si trova un utente senza privilegi
# invece del padrone del container. Ma il volume dati e' montato DOPO
# questo chown (a runtime, non al build) — vedi docker-entrypoint.sh per
# perche' il container deve comunque PARTIRE come root.
RUN useradd --system --uid 10001 --create-home appuser     && mkdir -p /srv/backend/data     && chown -R appuser:appuser /srv     && chmod +x /srv/docker-entrypoint.sh

WORKDIR /srv/backend
EXPOSE 8000
ENTRYPOINT ["/srv/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
