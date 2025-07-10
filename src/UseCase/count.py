from .fetch import fetch_inference_result

def count_people():
    # fetch.pyから推論結果を取得
    result = fetch_inference_result()
    
    # データがない場合は0を返す
    if result == 'No data':
        return 0
        
    # 人数をカウント (クラスIDが0のものをカウント)
    count = 0
    for key, value in result.items():
        if key != 'T' and value['C'] == 0:  # クラスID 0 は人物
            count += value['P']
            
    return int(round(count))
