# The assistant service (Part B): its own image, dependencies and process (spec D-17).
# Built from the repository root so the generated bindings and nothing else are copied.
FROM python:3.12-slim
WORKDIR /srv
COPY services/assistant/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY services/assistant/gen ./gen
COPY services/assistant/assistant_service ./assistant_service
EXPOSE 50051
HEALTHCHECK --interval=10s --timeout=5s --retries=12   CMD python -c "import grpc; from grpc_health.v1 import health_pb2 as h, health_pb2_grpc as g; c=grpc.insecure_channel('localhost:50051'); r=g.HealthStub(c).Check(h.HealthCheckRequest(), timeout=3); raise SystemExit(0 if r.status==1 else 1)"
CMD ["python", "-m", "assistant_service.main"]
