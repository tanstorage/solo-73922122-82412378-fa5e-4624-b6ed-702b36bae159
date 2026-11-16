FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY sample_survey.csv ./sample_survey.csv

ENV DB_PATH=/data/app.db
EXPOSE 8000

# 数据目录（由 compose 挂载卷持久化，重启保留公开记录）
VOLUME ["/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
