# Python 3.9のベースイメージを使用
FROM python:3.9-slim

# 作業ディレクトリを設定
WORKDIR /app

# システムの依存関係をインストール
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# requirements.txtをコピーして依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションのソースコードをコピー
COPY src/ ./src/

# 環境変数を設定
ENV PYTHONPATH=/app
ENV FLASK_APP=src/main.py
ENV FLASK_ENV=production

# ポート8000を公開
EXPOSE 8000

# アプリケーションを起動
CMD ["python", "src/main.py"] 