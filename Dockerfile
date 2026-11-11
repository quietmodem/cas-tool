# CAS 双云工具:Web 后台 + CLI
# 构建: docker build -t cas-tool .
# DS216+II 是 x86_64,官方 python alpine 镜像直接可用
FROM python:3.12-alpine

WORKDIR /app
COPY cas_tool.py cas_web.py ./

ENV PORT=8099 \
    CAS_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD wget -q -O /dev/null http://127.0.0.1:8099/healthz || exit 1

CMD ["python3", "cas_web.py"]
