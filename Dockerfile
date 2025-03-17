FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY rainscribe.py .
COPY *.md ./

# Expose the HTTP server port
EXPOSE 8080

# Volume for generated files
VOLUME ["/app/output"]

# Run the application
ENTRYPOINT ["python", "rainscribe.py"] 