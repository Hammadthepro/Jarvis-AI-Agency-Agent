FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /voice
COPY apps/voice/requirements.txt .
RUN pip install -r requirements.txt
COPY apps/voice/agent.py .
RUN useradd -r -u 10003 voice && python agent.py download-files || true
USER voice
CMD ["python", "agent.py", "start"]
