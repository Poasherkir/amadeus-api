FROM python:3.11-bullseye

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + all OS dependencies
RUN playwright install chromium --with-deps

COPY amadeus_ah.py .
COPY amadeus_api.py .

ENV PORT=8000
EXPOSE 8000

CMD ["python", "amadeus_api.py"]
