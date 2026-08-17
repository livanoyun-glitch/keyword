FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY dashboard.py trendyol_search.py panel.html ./

ENV HOST=0.0.0.0
ENV PORT=8765
ENV DATA_DIR=/app/data
ENV OPEN_BROWSER=0
ENV PLAYWRIGHT_DOCKER=1
ENV PYTHONUNBUFFERED=1

RUN mkdir -p /app/data

EXPOSE 8765

CMD ["python", "dashboard.py"]
