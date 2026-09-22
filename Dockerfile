FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY seeds/ ./seeds/

EXPOSE 4040

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4040"]