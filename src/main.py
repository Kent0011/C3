from flask import Flask
from flask import jsonify
from UseCase.fetch import fetch_inference_result
from UseCase.count import count_people
import threading
import time
import random # テスト用
import logging # log非表示
import datetime

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

people_count = 0
people_count_log = []
AVERAGE_STAY_MINUTES = 18.5
entry_history = []

# 祝日・平日判定関数
def is_business_day(date):
    return date.weekday() < 5  # 月〜金を営業日とする

# 滞在時間推定関数（グループ人数に応じた）
def estimate_stay_minutes(group_size):
    """
    グループ人数に応じた平均滞在時間（分）を返す。
    """
    stay_time_table = {
        1: 14,
        2: 20,
        3: 24,
        4: 23,
        5: 21,
        6: 19
    }
    return stay_time_table.get(group_size, 18.5)  # デフォルト18.5分

def count_up():
    global people_count, entry_history, people_count_log
    while True:
        delta = count_people()
        delta = random.randint(0, 3) # テスト用
        people_count += delta
        timestamp = time.strftime('%H:%M:%S')
        if time.time() % 20 < 3:
            people_count_log.append((timestamp, people_count))
        # if len(people_count_log) > 100:
        #     people_count_log.pop(0)
        time.sleep(3)

def count_down():
    global people_count
    while True:
        people_count -= 1
        people_count -= random.randint(0, 3) # テスト用
        if people_count < 0:
            people_count = 0
        time.sleep(60)

def activate_job():
    thread1 = threading.Thread(target=count_up)
    thread1.start()
    thread2 = threading.Thread(target=count_down)
    thread2.start()

@app.route('/api/count')
def api_count():
    try:
        congestion_rate = int(people_count / 360 * 100)
    except Exception as e:
        print(f"Error in /api/count: {e}")
        congestion_rate = -1
    return jsonify({
        "people_count": people_count,
        "congestion_rate": congestion_rate
    })

@app.route('/api/history')
def api_history():
    global people_count_log
    return jsonify({
        "labels": [t[0] for t in people_count_log],
        "data": [t[1] for t in people_count_log]
    })

@app.route('/api/yesterday')
def api_yesterday():
    today = datetime.date.today()
    offset = 1
    while True:
        target = today - datetime.timedelta(days=offset)
        if is_business_day(target):
            break
        offset += 1
    
    # 仮の30分刻みダミーデータ（本実装ではファイルやDBから取得して加工すべき）
    labels = []
    data = []
    for i in range(22):  # 11:00～19:30 → 30分刻みで22区間
        hour = 11 + (i // 2)
        minute = '00' if i % 2 == 0 else '30'
        labels.append(f"{hour:02d}:{minute}")
        data.append(random.randint(0, 400))  # 仮の人数データ
    return jsonify({
        "labels": labels,
        "data": data
    })

@app.route('/api/lastweek')
def api_lastweek():
    today = datetime.date.today()
    target = today - datetime.timedelta(days=7)
    while not is_business_day(target):
        target -= datetime.timedelta(days=1)

    labels = []
    data = []
    for i in range(22):  # 11:00～19:30
        hour = 11 + (i // 2)
        minute = '00' if i % 2 == 0 else '30'
        labels.append(f"{hour:02d}:{minute}")
        data.append(random.randint(0, 400))  # 仮の人数データ
    return jsonify({
        "labels": labels,
        "data": data
    })

@app.route('/')
def main():
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>食堂人数カウンター</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
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
            .chart-container {{
                width: 90vw;
                max-width: 800px;
                height: auto;
                margin: 0 auto;
            }}
            canvas {{
                width: 100% !important;
                height: auto !important;
            }}
        </style>
    </head>
    <body>
        <div class="counter" id="counter">{people_count}</div>
        <div class="message">人入りました</div>
        <div class="message" id="congestion">混雑率 {int(people_count/360*100)}%</div>
        <!-- <canvas id="peopleChart" width="600" height="300"></canvas> -->
        <div class="chart-container">
            <canvas id="peopleChart"></canvas>
        </div>
        <h3>昨日の人数推移（30分刻み）</h3>
        <div class="chart-container">
            <canvas id="yesterdayChart"></canvas>
        </div>
        <h3>1週間前の同曜日の人数推移（30分刻み）</h3>
        <div class="chart-container">
            <canvas id="lastWeekChart"></canvas>
        </div>
        <script>
            function updateData() {{
                fetch('/api/count')
                    .then(response => response.json())
                    .then(data => {{
                        document.getElementById('counter').textContent = data.people_count;
                        // document.getElementById('congestion').textContent = `混雑率 ${{data.congestion_rate}}%`;
                        
                        let rate = data.congestion_rate;
                        let color = '#4CAF50';
                        let message = `混雑率 ${{rate}}%`;

                        if (rate < 50) color = '#4CAF50';
                        else if (rate < 70) color = '#FFEB3B';
                        else if (rate < 90) color = '#FF9800';
                        else color = '#F44336';

                        // document.getElementById('congestion').textContent = message;
                        // document.getElementById('congestion').style.color = color;
                        const congestionElement = document.getElementById('congestion');
                        congestionElement.textContent = message;
                        congestionElement.style.color = color;
                    }});
            }}

            const ctx = document.getElementById('peopleChart').getContext('2d');
            const peopleChart = new Chart(ctx, {{
                type: 'bar',
                data: {{
                    labels: [],
                    datasets: [{{
                        label: '人数の推移',
                        data: [],
                        backgroundColor: 'rgba(75, 192, 192, 0.5)',
                        borderColor: 'rgba(75, 192, 192, 1)',
                        tension: 0,
                        cubicInterpolationMode: 'default'
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {{
                        x: {{
                            title: {{ display: true, text: '時刻' }}
                        }},
                        y: {{
                            beginAtZero: true,
                            title: {{ display: true, text: '人数' }}
                        }}
                    }}
                }}
            }});

            const yctx = document.getElementById('yesterdayChart').getContext('2d');
            const yesterdayChart = new Chart(yctx, {{
                type: 'bar',
                data: {{
                    labels: [],
                    datasets: [{{
                        label: '昨日の人数推移',
                        data: [],
                        backgroundColor: 'rgba(54, 162, 235, 0.5)',
                        borderColor: 'rgba(54, 162, 235, 1)',
                        tension: 0,
                        cubicInterpolationMode: 'default'
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {{
                        x: {{
                            title: {{ display: true, text: '時刻（30分ごと）' }}
                        }},
                        y: {{
                            beginAtZero: true,
                            title: {{ display: true, text: '人数' }}
                        }}
                    }}
                }}
            }});

            const lwctx = document.getElementById('lastWeekChart').getContext('2d');
            const lastWeekChart = new Chart(lwctx, {{
                type: 'bar',
                data: {{
                    labels: [],
                    datasets: [{{
                        label: '1週間前の人数推移',
                        data: [],
                        backgroundColor: 'rgba(255, 99, 132, 0.5)',
                        borderColor: 'rgba(255, 99, 132, 1)',
                        tension: 0,
                        cubicInterpolationMode: 'default'
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {{
                        x: {{
                            title: {{ display: true, text: '時刻（30分ごと）' }}
                        }},
                        y: {{
                            beginAtZero: true,
                            title: {{ display: true, text: '人数' }}
                        }}
                    }}
                }}
            }});

            function updateGraph() {{
                fetch('/api/history')
                    .then(response => response.json())
                    .then(history => {{
                        peopleChart.data.labels = history.labels;
                        peopleChart.data.datasets[0].data = history.data;
                        peopleChart.update();
                    }});
            }}

            function updateYesterdayGraph() {{
                fetch('/api/yesterday')
                    .then(response => response.json())
                    .then(history => {{
                        yesterdayChart.data.labels = history.labels;
                        yesterdayChart.data.datasets[0].data = history.data;
                        yesterdayChart.update();
                    }});
            }}

            function updateLastWeekGraph() {{
                fetch('/api/lastweek')
                    .then(response => response.json())
                    .then(history => {{
                        lastWeekChart.data.labels = history.labels;
                        lastWeekChart.data.datasets[0].data = history.data;
                        lastWeekChart.update();
                    }});
            }}

            setInterval(updateData, 3000);
            setInterval(updateGraph, 3000);

            window.onload = function() {{
                updateData();
                updateGraph();
                updateYesterdayGraph();
                updateLastWeekGraph();
            }};
        </script>
    </body>
    </html>
    '''

if __name__ == '__main__':
    activate_job()
    app.run(host='0.0.0.0', port=8000,debug=True)
