import requests
import json
from app.database import SessionLocal
from app.models import AppSetting

db = SessionLocal()
key_row = db.query(AppSetting).filter_by(key="genaipro_api_key").first()
db.close()

api_key = key_row.value
BASE_URL = "https://genaipro.vn/api/v1"
headers = {"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"}
form = {"prompt": "a beautiful landscape", "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE", "number_of_images": "1"}

print("--- Testing form-encoded ---")
try:
    resp = requests.post(f"{BASE_URL}/veo/create-image", headers=headers, data=form, stream=True, timeout=5)
    print("Form Status:", resp.status_code)
    for line in resp.iter_lines():
        print("Form line:", line.decode())
        if line: break
except Exception as e:
    print("Form error:", e)

print("\n--- Testing json ---")
try:
    resp2 = requests.post(f"{BASE_URL}/veo/create-image", headers=headers, json=form, stream=True, timeout=5)
    print("JSON Status:", resp2.status_code)
    for line in resp2.iter_lines():
        print("JSON line:", line.decode())
        if line: break
except Exception as e:
    print("JSON error:", e)
