import requests
import time

def wait_for_service(max_retries=10):
    for i in range(max_retries):
        try:
            response = requests.get("http://localhost:8000/health")
            if response.status_code == 200:
                print("Service is ready!")
                return True
        except:
            pass
        time.sleep(1)
    return False