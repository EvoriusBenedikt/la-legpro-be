# Multi-stage build (image slimming, post-M5):
#   stage 1 (builder)  -- carries build-essential + pip's compile/install work;
#                         only the installed tree (/install) leaves this stage.
#   stage 2 (runtime)  -- clean python:3.12-slim + the packages copied from
#                         the builder. (R1: the libgl1/libglib2.0-0 apt libs
#                         that used to sit here were for OpenCV, a PaddleOCR
#                         dependency -- both left with the dead OCR chain.)
# Both stages MUST stay on the same base tag: identical interpreter paths make
# /install merge cleanly into /usr/local and console-script shebangs resolve.

# ---- Stage 1: builder ----
FROM python:3.12-slim AS builder

# build-essential compiles any pin that ships no wheel; the multi-stage split
# keeps it (and all pip build intermediates) out of the runtime image
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY api/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Stage 2: runtime ----
FROM python:3.12-slim

# (R1: this stage used to apt-install libgl1 + libglib2.0-0 for OpenCV, a
# PaddleOCR dependency -- the opencv-contrib-python pin and the whole paddle
# chain are gone, so the runtime stage needs no apt packages at all.)

# Installed packages + console scripts (uvicorn et al.) from the builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy the rest of the application
COPY . .

# Set PYTHONPATH so absolute imports work if needed
ENV PYTHONPATH=/app

# Expose the API port
EXPOSE 8000

# Run the FastAPI application
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
