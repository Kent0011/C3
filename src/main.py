from flask import Flask
from UseCase.fetch import fetch_inference_result
from UseCase.count import count_people
import threading
import time

app = Flask(__name__)

people_count = 0

def background_job():
    global people_count
    while True:
        people_count += count_people()
        time.sleep(2)

def activate_job():
    thread = threading.Thread(target=background_job)
    thread.start()

@app.route('/')
def main():
    return str(people_count)

if __name__ == '__main__':
    activate_job()
    app.run(host='0.0.0.0', port=8000)
