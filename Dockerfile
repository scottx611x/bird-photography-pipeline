FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bird_post.py .
COPY server.py .
COPY templates/ templates/
COPY static/ static/

# Volumes mounted at runtime:
#   /downloads  ← ~/Downloads
#   /staging    ← ~/Desktop/bird_staging
#   /birds      ← ~/Desktop/birbs

ENV FLASK_APP=server.py
ENV PYTHONUNBUFFERED=1
EXPOSE 8765

CMD ["python", "server.py"]
