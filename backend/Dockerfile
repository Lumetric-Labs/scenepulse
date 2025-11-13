# Use lightweight Python base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy dependency list and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Tell Cloud Run which port to listen on
ENV PORT=8080
EXPOSE 8080

# ✅ Correct way to launch FastAPI in Cloud Run
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
