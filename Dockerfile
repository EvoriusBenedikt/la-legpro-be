FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (build-essential needed for some python packages like bcrypt, chromadb)
# libgl1 and libglib2.0-0 are required by OpenCV (cv2) which is used by PaddleOCR
RUN apt-get update && apt-get install -y \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Set PYTHONPATH so absolute imports work if needed
ENV PYTHONPATH=/app

# Expose the API port
EXPOSE 8000

# Run the FastAPI application
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
