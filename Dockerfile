FROM python:3.11-slim
WORKDIR /app
COPY app.py ./
COPY templates ./templates
ENV PYTHONUNBUFFERED=1
CMD ["python3", "app.py"]
