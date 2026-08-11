FROM python:3.14-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY VERSION server.py watchlist_db.py watchlist_ingest.py watchlist_pages.py price_sidecar.py watchlist_words.txt og.png rulebook.py MagicCompRules.txt ./
COPY goldfish/ goldfish/

EXPOSE 8000
CMD ["python", "server.py"]
