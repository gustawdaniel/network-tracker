FROM python:3.11-alpine

WORKDIR /app

COPY src/ /app/src/
COPY web/ /app/web/

ENV PORT=8780
ENV CLICKHOUSE_URL=http://localhost:8123

EXPOSE 8780

CMD ["python3", "src/server.py", "--port", "8780"]
