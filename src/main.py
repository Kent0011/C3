from flask import Flask
from flask import jsonify
from UseCase.fetch import fetch_inference_result
from UseCase.count import count_people
import threading
import time
import random # テスト用
import logging # log非表示
import datetime
from collections import deque
import itertools

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

@app.route('/')
def index():
    return fetch_inference_result()

@app.route('/ping')
def ping():
    return 'pong'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000,debug=True)
