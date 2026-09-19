FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# Install basic OS prerequisites
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Install Playwright Chromium and required OS dependencies as root
RUN python -m playwright install --with-deps chromium

# Copy application source
COPY . /app

EXPOSE 8000

CMD ["sh", "-c", "python backend/manage.py migrate && cd backend && gunicorn tracker_project.wsgi:application --bind 0.0.0.0:${PORT:-8000} --timeout 180 --workers 2 --threads 4"]
