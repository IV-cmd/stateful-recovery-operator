FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY recovery_operator/ recovery_operator/

RUN pip install --no-cache-dir .

RUN useradd --system --uid 10001 sro
USER sro

CMD ["kopf", "run", "--all-namespaces", "-m", "recovery_operator.handlers"]
