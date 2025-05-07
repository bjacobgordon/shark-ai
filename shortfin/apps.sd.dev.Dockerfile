FROM python:3.12-slim
WORKDIR /service

COPY pyproject.toml .
RUN pip install --no-cache-dir --editable .[dev]

ENV PORT=8000
CMD ["sh", "-c", "python -m shortfin_apps.sd.server --host 0.0.0.0 --port ${PORT}"]
