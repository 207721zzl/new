ARG PYTHON_IMAGE=m.daocloud.io/docker.io/pytorch/pytorch:2.12.1-cuda12.6-cudnn9-runtime
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential curl tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --break-system-packages --upgrade pip \
    && python -m pip install --break-system-packages -r requirements.txt

COPY alembic.ini main.py ./
COPY alembic ./alembic
COPY app ./app
COPY data ./data
COPY scripts ./scripts

RUN mkdir -p /app/data/knowledge/uploads /app/logs /app/reports

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
