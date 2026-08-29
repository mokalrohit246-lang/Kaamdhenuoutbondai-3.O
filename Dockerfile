FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Ensure start.sh has executable permissions and LF line endings
RUN chmod +x start.sh || true

# Expose HTTP port 8000 for Coolify / Traefik reverse proxy
EXPOSE 8000

# Entrypoint using background supervisor start.sh
CMD ["/bin/bash", "start.sh"]
