FROM python:3.12-slim

WORKDIR /app

# Install Python packages first
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + ALL its system dependencies in one step
# --with-deps handles every OS package automatically
RUN playwright install chromium --with-deps

# Copy source files
COPY amadeus_ah.py .
COPY amadeus_api.py .

ENV PORT=8000
EXPOSE 8000

CMD ["python", "amadeus_api.py"]
