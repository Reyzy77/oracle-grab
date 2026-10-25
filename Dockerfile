FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY oci_grab.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "oci_grab.py", "run"]
