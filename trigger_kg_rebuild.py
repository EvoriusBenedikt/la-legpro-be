"""
One-time script to trigger the KG rebuild API for all existing documents.
Run AFTER restarting main.py.
"""
import requests, json

BASE_URL = "http://localhost:8000"

# Step 1: Login as admin to get a token
print("Logging in as admin...")
resp = requests.post(f"{BASE_URL}/api/auth/login", json={"username": "admin", "password": "@Admin123"})
if not resp.ok:
    print(f"Login failed: {resp.status_code} {resp.text}")
    exit(1)

token = resp.json().get("access_token")
print(f"Got token: {token[:30]}...")

# Step 2: Trigger the rebuild
print("Triggering KG rebuild for all existing documents...")
rebuild_resp = requests.post(
    f"{BASE_URL}/api/knowledge-graph/rebuild",
    headers={"Authorization": f"Bearer {token}"}
)
print(f"Status: {rebuild_resp.status_code}")
print(f"Response: {rebuild_resp.text}")
print("\nRebuild is running in background. Monitor server.log for [KG Rebuild] progress.")
