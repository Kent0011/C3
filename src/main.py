from flask import Flask
import Repository
import logging
import os
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

ai_camera_repository = Repository.AiCameraRepository(
    console_endpoint=os.getenv("CONSOLE_ENDPOINT"),
    auth_endpoint=os.getenv("AUTH_ENDPOINT"),
    client_id=os.getenv("CLIENT_ID"),
    client_secret=os.getenv("CLIENT_SECRET"),
    device_id=os.getenv("DEVICE_ID")
)

@app.route('/')
def index():
    """
    カメラの推論結果をjsonで返す
    """
    return ai_camera_repository.fetch_inference_result()

@app.route('/ping')
def ping():
    """
    pingを返す
    """
    return 'pong'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
