FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GREYBOT_STATE_DIR=/state
WORKDIR /app/control
COPY control/requirements.txt control/requirements-voice.txt ./
# libopus decodes received voice; discord.py loads the system library through ctypes.
RUN apt-get update && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --no-deps -r requirements-voice.txt \
    && useradd --create-home --uid 10001 greybot \
    && mkdir /state && chown greybot:greybot /state
COPY control/greybot_control ./greybot_control
COPY assets/greyBot-avatar-v5.png /app/assets/greyBot-avatar-v5.png
COPY assets/tenure-badges /app/assets/tenure-badges
COPY assets/voice /app/assets/voice
RUN chmod -R a+rX /app/control /app/assets
USER greybot
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "greybot_control.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
