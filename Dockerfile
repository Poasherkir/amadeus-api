# Official Microsoft image — Chromium already installed, nothing to download
FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY amadeus_ah.py .
COPY amadeus_api.py .

ENV PORT=8000
EXPOSE 8000

CMD ["python", "amadeus_api.py"]
