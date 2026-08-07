FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Esta línea es la que evita el crasheo de Playwright
RUN playwright install chromium --with-deps
COPY . .
ENV PORT=8080
EXPOSE $PORT
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
