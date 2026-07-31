FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

COPY config ./config
COPY examples ./examples

RUN useradd --create-home --uid 10001 swellsign \
    && mkdir -p /app/data \
    && chown -R swellsign:swellsign /app/data

USER swellsign

EXPOSE 8000
CMD ["swellsign", "api", "--host", "0.0.0.0"]

