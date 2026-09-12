FROM python:3.12-slim

# 所有运行期产物都写到 /data（由 compose bind mount 到 E:\2bt0 project\data）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PYTHONPYCACHEPREFIX=/data/tmp/pycache \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"

# 必须单 worker：应用在模块级启动后台线程（断点自动续抓 / 每日定时增量 /
# 详情拉取），多 worker 会得到多份互相冲突的后台任务并重复请求站点
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
