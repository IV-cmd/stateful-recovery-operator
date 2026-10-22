FROM python:3.11-slim@sha256:cce12316475d7d56d605f76e1d811d0dc4e7227f2d4afd29951521264f504ff8

WORKDIR /app

COPY pyproject.toml README.md ./
COPY recovery_operator/ recovery_operator/

RUN pip install --no-cache-dir .

RUN useradd --system --uid 10001 sro
# numeric uid so runAsNonRoot can verify it
USER 10001

CMD ["kopf", "run", "--standalone", "-m", "recovery_operator.handlers"]
