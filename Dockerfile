FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE server.py ./
COPY tests ./tests

RUN pip install --no-cache-dir -e .

ENV LIGGGHTS_RUNS=/root/liggghts_runs

VOLUME ["/root/liggghts_runs"]

CMD ["python", "/app/server.py"]
