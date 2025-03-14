FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install dependencies
RUN pip install poetry==1.7.1
RUN poetry config virtualenvs.create false

# Copy requirements
COPY pyproject.toml poetry.lock* /app/

# Install dependencies
RUN poetry install --no-dev --no-interaction --no-ansi

# Copy shared modules
COPY shared/ /app/shared/

# Copy application code
COPY transcription-service/ /app/
COPY .env /app/.env

# Install requirements for shared modules
COPY requirements.txt /app/
RUN pip install -r requirements.txt

# Set execute permissions
RUN chmod +x /app/main.py

# Command to run the application
CMD ["python", "/app/main.py"] 