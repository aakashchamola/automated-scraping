import requests, json, re
from bs4 import BeautifulSoup

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

for company, slug in [('Microsoft', 'microsoft'), ('AstraZeneca', 'astrazeneca')]:
    url = f'https://www.linkedin.com/company/{slug}/about'
    try:
        resp = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
        print(f'{company}: status={resp.status_code} final_url={resp.url!r}')
        if "login" in resp.url or "authwall" in resp.url:
            print("  Status: Redirected to login/authwall")
        else:
            soup = BeautifulSoup(resp.text, 'html.parser')
            # Look for JSON-LD first
            found_json = False
            for script in soup.find_all('script', {'type': 'application/ld+json'}):
                try:
                    data = json.loads(script.string or '')
                    if isinstance(data, dict) and 'numberOfEmployees' in data:
                        print(f'  JSON-LD employees: {data["numberOfEmployees"]}')
                        found_json = True
                except: pass
            
            # Look for regex match
            text = soup.get_text(' ', strip=True)
            match = re.search(r'([\d,]+\+?)\s*employees', text, re.IGNORECASE)
            if match:
                print(f'  Regex match: {match.group(0)}')
            elif not found_json:
                print(f'  No employee count found (text len={len(text)})')
                print(f'  Preview: {text[:200]!r}')
    except Exception as e:
        print(f'{company}: ERROR {e}')
