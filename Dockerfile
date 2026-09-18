ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=100

WORKDIR /app

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin arachne

COPY requirements.txt .

ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      if [ -n "$PIP_TRUSTED_HOST" ]; then \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" -r requirements.txt; \
      else \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt; \
      fi; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY app ./app

RUN mkdir -p /app/data && chown arachne:arachne /app/data

USER arachne
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
