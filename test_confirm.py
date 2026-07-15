import requests

headers = {
    "Origin": "https://legal-analyzer.lintasarta.dev",
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization,content-type"
}
res = requests.options("http://localhost:8000/api/repository/pending/972/confirm", headers=headers)
print("OPTIONS:", res.status_code, res.headers)
