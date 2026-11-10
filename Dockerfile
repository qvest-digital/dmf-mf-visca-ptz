FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates/ templates/

USER 65532:65532
ENV PYTHONUNBUFFERED=1
# 5000: web UI, Socket.IO and the HTTP API. 52381/udp: Sony VISCA-over-IP replies.
EXPOSE 5000 52381/udp
CMD ["python", "app.py"]
