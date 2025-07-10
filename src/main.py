from flask import Flask
import UseCase.fetch as fetch

app = Flask(__name__)

@app.route('/')
def main():
    return fetch.fetch_inference_result()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8000)
