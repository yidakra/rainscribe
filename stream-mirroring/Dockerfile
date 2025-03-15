FROM python:3.10-slim

# Install FFmpeg and other dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Poetry
RUN pip install poetry==1.7.1

# Configure poetry
RUN poetry config virtualenvs.create false

# Copy only requirements to cache them in docker layer
COPY pyproject.toml poetry.lock* /app/

# Install dependencies
RUN poetry install --no-dev --no-interaction --no-ansi

# Create scripts directory
RUN mkdir -p /app/scripts

# Copy shared modules
COPY shared/ /app/shared/

# Copy application code
COPY stream-mirroring/ /app/
COPY .env /app/.env

# Copy subtitle symlinks update scripts
COPY scripts/update_subtitle_symlinks.sh /app/scripts/
COPY scripts/update_subtitle_symlinks_loop.sh /app/scripts/
COPY scripts/subtitle_symlink_service.sh /app/scripts/

# Install requirements for shared modules
COPY requirements.txt /app/
RUN pip install -r requirements.txt

# Set execute permissions
RUN chmod +x /app/main.py
RUN chmod +x /app/scripts/update_subtitle_symlinks.sh
RUN chmod +x /app/scripts/update_subtitle_symlinks_loop.sh
RUN chmod +x /app/scripts/subtitle_symlink_service.sh

# Command to run the application
CMD ["python", "/app/main.py"] 