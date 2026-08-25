# pdfplumber is pure Python (pdfminer.six), so no system packages are needed.
# If the OCR tier is added later, this is where tesseract-ocr goes.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY profiles/ ./profiles/

# Never run as root: this process parses untrusted files from the internet.
RUN useradd -m -u 10001 app && chown -R app:app /app
USER app

ENV MCP_TRANSPORT=http HOST=0.0.0.0 PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["python", "-m", "src.server"]
