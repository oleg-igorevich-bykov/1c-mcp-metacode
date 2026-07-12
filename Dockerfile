# Base image
FROM python:3.12-slim

# Environment settings
# MALLOC_ARENA_MAX limits glibc malloc arenas (read before Python starts) so the
# process does not fragment across dozens of arenas and retain freed memory after
# peak allocations. Overridable via docker-compose; recreate the container to apply.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=4

# Working directory inside the container
WORKDIR /app

# System deps:
#   curl                — runtime utility (existing)
#   gcc, build-essential — required to compile the tree-sitter-bsl C extension
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    curl gcc build-essential \
 && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Copy only requirements first to leverage Docker layer caching
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Install local tree-sitter-bsl package (C extension; needs gcc above)
COPY third_party/tree-sitter-bsl /tmp/tree-sitter-bsl
RUN pip install --no-cache-dir /tmp/tree-sitter-bsl

# Copy application source code
COPY app/ /app

# Prepare runtime directories (logs/config; data volume is mounted at runtime)
RUN mkdir -p /app/logs /app/config

# Expose MCP server port
EXPOSE 6001

# Default command
CMD ["python", "main.py"]
