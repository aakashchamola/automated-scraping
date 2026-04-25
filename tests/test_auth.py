import requests
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}
url = 'https://www.linkedin.com/company/microsoft/about'
try:
    resp = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
    print(f'Microsoft: status={resp.status_code} final_url={resp.url!r}')
    if "login" in resp.url or "authwall" in resp.url:
        print("  Redirected to login/authwall")
    else:
        print(f"  Page content length: {len(resp.text)}")
except Exception as e:
    print(f'ERROR: {e}')
