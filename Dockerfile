FROM python:3.12-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
# 服务器直连 PyPI 会卡死（实测 8s 超时），必须走清华镜像源
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY app ./app
COPY scripts ./scripts

# 非 root 运行；数据目录挂载卷
RUN useradd -m clerk && mkdir -p /app/data && chown -R clerk:clerk /app
USER clerk

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
