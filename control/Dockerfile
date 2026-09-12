FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GREYBOT_STATE_DIR=/state
ENV PYTHONPATH=/app/recap FONT_DIR=/app/assets/fonts
WORKDIR /app/control
COPY control/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 greybot \
    && mkdir /state && chown greybot:greybot /state
COPY control/greybot_control ./greybot_control
COPY assets/greyBot-avatar-v4.png /app/assets/greyBot-avatar-v4.png
COPY assets/fonts /app/assets/fonts
COPY src/recap_card.py src/recap_page.py src/kill_card.py /app/recap/
RUN chmod -R a+rX /app/control /app/assets /app/recap
USER greybot
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "greybot_control.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
