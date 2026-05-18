# ==============================================================
# Alibaba Autoresponder & Agentic AI Framework - Docker Container
# ==============================================================
FROM python:3.11-slim

# Install system dependencies required by Google Chrome and Xvfb (Virtual Framebuffer)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    unzip \
    curl \
    xvfb \
    procps \
    # Chrome runtime dependencies
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libxshmfence1 \
    libx11-xcb1 \
    libxcb1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrender1 \
    libxtst6 \
    libx11-6 \
    fonts-liberation \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Add Google Chrome's signing key and official repository
RUN curl -fSsL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV CHROME_BIN=/usr/bin/google-chrome
ENV DISPLAY=:99

WORKDIR /app

# Copy Python requirements first (to exploit Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project code
COPY ["ali_msg 1.py", "."]
COPY entrypoint.sh .
COPY app/ ./app/

# Make the entrypoint script executable
RUN chmod +x entrypoint.sh

# Render/container default port exposition
EXPOSE 10000

# Execute entrypoint
CMD ["./entrypoint.sh"]
