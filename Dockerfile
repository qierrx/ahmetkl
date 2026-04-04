FROM python:3.11-slim

# ffmpeg kur (video+ses birleştirme için şart)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bağımlılıkları kur
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -U yt-dlp

# Proje dosyalarını kopyala
COPY . .

EXPOSE 10000

CMD ["python", "server.py"]
