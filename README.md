# SmartCamera Flask API

オブジェクト検出の推論結果を取得するFlask APIアプリケーションです。

## ローカル実行

```bash
$ python main.py
```

## Docker実行

### 1. 環境変数の設定

`env.example`を参考に`.env`ファイルを作成してください：

```bash
cp env.example .env
# .envファイルを編集して実際の値を設定
```

### 2. Docker Composeで起動

```bash
# アプリケーションをビルドして起動
docker-compose up --build

# バックグラウンドで実行
docker-compose up -d --build
```

### 3. アクセス

アプリケーションは `http://localhost:8000` でアクセスできます。

## 環境変数

- `CONSOLE_ENDPOINT`: コンソールエンドポイントのURL
- `AUTH_ENDPOINT`: 認証エンドポイントのURL
- `CLIENT_ID`: クライアントID
- `CLIENT_SECRET`: クライアントシークレット
- `DEVICE_ID`: デバイスID
