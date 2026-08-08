FROM python:3.14-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py watchlist_db.py watchlist_ingest.py watchlist_pages.py watchlist_words.txt og.png ./
COPY goldfish/ goldfish/

EXPOSE 8000
CMD ["python", "server.py"]
