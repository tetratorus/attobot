FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
WORKDIR /app
COPY requirements.txt lab-constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c lab-constraints.txt \
    && useradd --uid 1000 --no-create-home attobot \
    && mkdir /work && chown attobot:attobot /work
COPY agent.py setup.py SOUL.md lab.py lab.html test_agent.py ./
COPY opt/ ./opt/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/work
USER 1000:1000
WORKDIR /work
EXPOSE 8080
ENTRYPOINT ["python", "/app/lab.py"]
