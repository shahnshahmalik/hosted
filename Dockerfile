FROM python:3.12-slim
LABEL org.opencontainers.image.title="Hosted" \
      org.opencontainers.image.description="Self-hosted dashboard with an AI that builds widgets, layouts and themes" \
      org.opencontainers.image.source="https://github.com/shahnshahmalik/hosted" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY static static
COPY widgets widgets
COPY themes themes
COPY defaults defaults
ENV CANVAS_DB=/data/canvas.db
VOLUME /data
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
