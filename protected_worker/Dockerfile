FROM python:3.12-slim
RUN pip install --no-cache-dir cryptography==46.0.3
COPY server.py task.py /app/
WORKDIR /app
USER 1000
CMD ["python", "-u", "server.py"]
