# DebianベースのPythonイメージを使用
FROM python:3.11-slim

# FFmpegと依存関係をインストール
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# 作業ディレクトリの設定
WORKDIR /app

# 依存関係のインストール
COPY requirements.txt .
RUN pip install -r requirements.txt

# アプリケーションコードをコピー
COPY . .

# Uvicornを使ってFastAPIアプリケーションを起動
# Renderの環境では、ポートは環境変数で設定されます（通常は10000）
CMD ["uvicorn", "api.index:app", "--host", "0.0.0.0", "--port", "8000"]
