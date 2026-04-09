FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY src/ src/

ENV PORT=8080
EXPOSE 8080

CMD ["python", "-m", "gdrive_mcp"]
