FROM python:3.12-slim

WORKDIR /app

# curl is needed by the docker healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy.py .

EXPOSE 8000

CMD ["python", "proxy.py"]
