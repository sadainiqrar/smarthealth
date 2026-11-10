FROM python:3.12-slim
WORKDIR /srv
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.32"
COPY __init__.py app.py ./services/payments_mock/
RUN touch ./services/__init__.py
EXPOSE 8090
HEALTHCHECK --interval=5s --timeout=3s --retries=12 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8090/health')"
CMD ["uvicorn", "services.payments_mock.app:app", "--host", "0.0.0.0", "--port", "8090"]
