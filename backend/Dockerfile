# Base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy files
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose port for Cloud Run
ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port $PORT
