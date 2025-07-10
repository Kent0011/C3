from flask import Flask
from UseCase.fetch import fetch_inference_result
from UseCase.count import count_people
import threading
import time

app = Flask(__name__)

people_count = 0

def count_up():
    global people_count
    while True:
        people_count += count_people()
        time.sleep(2)

def count_down():
    global people_count
    while True:
        people_count -= 1
        time.sleep(60)

def activate_job():
    thread1 = threading.Thread(target=count_up)
    thread1.start()
    thread2 = threading.Thread(target=count_down)
    thread2.start()

@app.route('/')
def main():
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>食堂人数カウンター</title>
        <meta charset="utf-8">
        <style>
            body {{
                font-family: Arial, sans-serif;
                text-align: center;
                margin-top: 50px;
                background-color: #f0f0f0;
            }}
            .counter {{
                font-size: 48px;
                color: #333;
                margin: 20px 0;
            }}
            .message {{
                font-size: 24px;
                color: #666;
            }}
        </style>
    </head>
    <body>
        <div class="counter">{people_count}</div>
        <div class="message">人入りました</div>
    </body>
    </html>
    '''

if __name__ == '__main__':
    activate_job()
    app.run(host='0.0.0.0', port=8000)
