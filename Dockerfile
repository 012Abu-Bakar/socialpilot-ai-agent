FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SOCIALPILOT_MOCK_MODE=1 SOCIALPILOT_DB=/data/socialpilot.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /data && chown app /data
# Copy only the application; secrets (.env), local databases and virtualenvs never enter the image.
COPY main.py .
COPY static ./static
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
