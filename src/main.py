from flask import Flask
import Repository
import logging

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

@app.route('/')
def index():
    """
    カメラの推論結果をjsonで返す
    """
    return Repository.ai_camera_repository.fetch_inference_result()

@app.route('/ping')
def ping():
    """
    pingを返す
    """
    return 'pong'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
