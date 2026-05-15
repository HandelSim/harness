# Mock upstream LLM API for benchmark wiring tests.
# See app.py for the supported env vars.
FROM python:3.13-slim

RUN pip install --no-cache-dir flask==3.0.* gunicorn==23.0.*

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 80
# threaded=True is set in app.py; for higher load swap to gunicorn here.
CMD ["python", "/app/app.py"]
