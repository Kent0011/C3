import requests
import base64
import json
import sys
import os
from dotenv import load_dotenv
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from SmartCamera import ObjectDetectionTop
from SmartCamera import BoundingBox
from SmartCamera import BoundingBox2d


def fetch_inference_result():

    # 環境変数を読み込み
    load_dotenv()
    
    # API設定
    console_endpoint = os.getenv("CONSOLE_ENDPOINT")
    auth_endpoint = os.getenv("AUTH_ENDPOINT")
    client_id = os.getenv("CLIENT_ID")
    client_secret = os.getenv("CLIENT_SECRET")
    device_id = os.getenv("DEVICE_ID")
    
    # 1. アクセストークン取得
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        'authorization': f'Basic {auth}',
        'content-type': 'application/x-www-form-urlencoded'
    }
    data = 'grant_type=client_credentials&scope=system'
    
    response = requests.post(auth_endpoint, headers=headers, data=data)
    # print(response.json())
    access_token = response.json()['access_token']
    
    # 2. 推論結果取得
    headers = {'Authorization': f'Bearer {access_token}'}
    url = f"{console_endpoint}/inferenceresults/devices/{device_id}?limit=1&scope=full"
    
    response = requests.get(url, headers=headers)
    
    if len(response.json()['data']) == 0:
        return 'No data'
    
    buf = response.json()['data'][0]['inference_result']
    
    # Base64 でデコード
    if 'O' in buf['Inferences'][0]:
        buf_decode = base64.b64decode(buf['Inferences'][0]['O'])
    else:
        with open('decoded_result_ObjectDetection.json', 'w', encoding='utf-8') as file:
            json.dump(buf, file, ensure_ascii=False, indent=4)
    
    # デコードしたデータをflatbuffersでデシリアライズ
    ppl_out = ObjectDetectionTop.ObjectDetectionTop.GetRootAsObjectDetectionTop(buf_decode, 0)
    obj_data = ppl_out.Perception()
    res_num = obj_data.ObjectDetectionListLength()
    
    # 逆シリアライズしたデータをjson形式で保存
    buf['Inferences'][0].pop('O')
    for i in range(res_num):
        obj_list = obj_data.ObjectDetectionList(i)
        union_type = obj_list.BoundingBoxType()
        if union_type == BoundingBox.BoundingBox.BoundingBox2d:
            bbox_2d = BoundingBox2d.BoundingBox2d()
            bbox_2d.Init(obj_list.BoundingBox().Bytes, obj_list.BoundingBox().Pos)
            buf['Inferences'][0][str(i + 1)] = {}
            buf['Inferences'][0][str(i + 1)]['C'] = obj_list.ClassId()
            buf['Inferences'][0][str(i + 1)]['P'] = obj_list.Score()
            buf['Inferences'][0][str(i + 1)]['X'] = bbox_2d.Left()
            buf['Inferences'][0][str(i + 1)]['Y'] = bbox_2d.Top()
            buf['Inferences'][0][str(i + 1)]['x'] = bbox_2d.Right()
            buf['Inferences'][0][str(i + 1)]['y'] = bbox_2d.Bottom()
    
    return buf['Inferences'][0]