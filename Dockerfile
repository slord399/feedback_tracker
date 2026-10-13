FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Entry point will be set in docker-compose or overridden
CMD ["python", "Bot/poller/main.py"]
