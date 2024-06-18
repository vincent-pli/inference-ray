jsonData = [{"prompt": 'what should I hate '}]
url = "http://127.0.0.1:8000/api/v1/default/facebook--opt-125m/run/stream"

import json, requests
# response = requests.post(url=url, data='[{"prompt": "Tell me a story about cats."}]', stream=True) 
response = requests.post(url=url, json=jsonData, stream=True) 
response.raise_for_status() 
for chunk in response.iter_content(chunk_size=None, decode_unicode=True): 
    print("----") 
    print(chunk, end="")



# import requests

# prompt = "Tell me a story about cats."

# response = requests.post(f"http://localhost:8000/?prompt={prompt}", stream=True)
# response.raise_for_status()
# for chunk in response.iter_content(chunk_size=None, decode_unicode=True):
# 	print("----")
# 	print(chunk, end="")
