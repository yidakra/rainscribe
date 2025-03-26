FROM python:3.11-slim

# Install dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set up app directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user to run the app
RUN groupadd -r rainscribe && \
    useradd -r -g rainscribe rainscribe

# Create output directory with proper permissions
RUN mkdir -p /app/output && \
    chmod 777 /app/output && \
    chown -R rainscribe:rainscribe /app

# Copy application code
COPY . .
RUN chown -R rainscribe:rainscribe /app

# Expose port for HTTP server
EXPOSE 8080

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV OUTPUT_DIR=/app/output
ENV CAPTIONS_LOG_LEVEL=INFO
ENV SYSTEM_LOG_LEVEL=INFO
ENV TRANSCRIPTION_LOG_LEVEL=ERROR

# Switch to non-root user
USER rainscribe

# Run the application
CMD ["python", "rainscribe.py"]