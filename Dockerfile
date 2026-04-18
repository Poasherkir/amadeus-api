# Official Playwright image – Chromium + all system deps pre-installed
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser inside the image
RUN playwright install chromium

# Copy source files
COPY amadeus_ah.py .
COPY amadeus_api.py .

# Railway injects $PORT at runtime (default 8000 locally)
ENV PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "python amadeus_api.py"]
