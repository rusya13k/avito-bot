# Dockerfile
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies (if any needed for compiling sqlite/extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Graceful shutdown: docker stop sends SIGTERM → signal handler joins threads
STOPSIGNAL SIGTERM

# Command to run the bot
CMD ["python", "-u", "bot.py"]
