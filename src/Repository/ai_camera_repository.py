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

class AiCameraRepository:
    def __init__(self, console_endpoint: str, auth_endpoint: str, client_id: str, client_secret: str, device_id: str):
        self.console_endpoint = console_endpoint
        self.auth_endpoint = auth_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.device_id = device_id

    def fetch_inference_result(self) -> dict:
        """
        カメラの推論結果を成形して返す
        """
        
        # 1. アクセストークン取得
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        headers = {
            'authorization': f'Basic {auth}',
            'content-type': 'application/x-www-form-urlencoded'
        }
        data = 'grant_type=client_credentials&scope=system'
        
        response = requests.post(self.auth_endpoint, headers=headers, data=data)
        access_token = response.json()['access_token']
        
        # 2. 推論結果取得
        headers = {'Authorization': f'Bearer {access_token}'}
        url = f"{self.console_endpoint}/inferenceresults/devices/{self.device_id}?limit=1&scope=full"
        
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            return {"message": "Failed to get inference result"}
        
        if len(response.json()['data']) == 0:
            return {}
        
        buf = response.json()['data'][0]['inference_result']
        
        # Base64 でデコード
        if 'O' in buf['Inferences'][0]:
            buf_decode = base64.b64decode(buf['Inferences'][0]['O'])
        else:
            return {}
        
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
    
    def fetch_dummy_result(self) -> dict:
        return 1