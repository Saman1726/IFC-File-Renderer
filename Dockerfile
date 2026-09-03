# ---------------------------------------------------------------------------
# CERP IFC Renderer - Multi-stage Docker image
# Stage 1: System deps + Blender
# Stage 2: Python runtime
# ---------------------------------------------------------------------------
FROM ubuntu:24.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip fontconfig \
    libgl1 libegl1 libxrender1 libxcursor-dev \
    libxext6 libx11-6 libxxf86vm1 \
    python3.12 python3.12-venv python3.12-dev python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install Blender via apt
ARG BLENDER_VERSION=4.0.2
RUN apt-get update && apt-get install -y --no-install-recommends \
        blender=${BLENDER_VERSION}+dfsg-1ubuntu8 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/blender /usr/local/bin/blender \
    && blender --version

# Stage 2: Python application
FROM ubuntu:24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV BLENDER_PATH=/usr/bin/blender

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libgl1 libegl1 libxrender1 libxcursor-dev \
    libxext6 libx11-6 libxxf86vm1 \
    python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Install Blender in runtime stage
ARG BLENDER_VERSION=4.0.2
RUN apt-get update && apt-get install -y --no-install-recommends \
        blender=${BLENDER_VERSION}+dfsg-1ubuntu8 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/blender /usr/local/bin/blender

# Python venv
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY src/ ./src/

# Cache dir
RUN mkdir -p /app/cache

EXPOSE 8093

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8093", "--workers", "2"]
