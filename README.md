# C32025前期
カメラの推論結果をjsonで返すAPI

## ローカル実行

```bash
$ python main.py
```

## Docker実行

```bash
# バックグラウンドで実行
docker-compose up -d --build
```

### 3. アクセス

アプリケーションは `http://localhost:8000` でアクセス

## 環境変数

- `CONSOLE_ENDPOINT`: コンソールエンドポイントのURL
- `AUTH_ENDPOINT`: 認証エンドポイントのURL
- `CLIENT_ID`: クライアントID
- `CLIENT_SECRET`: クライアントシークレット
- `DEVICE_ID`: デバイスID
