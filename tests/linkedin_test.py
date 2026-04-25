import requests, json, re
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, unquote

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
}

for company, slug in [('Microsoft', 'microsoft'), ('AstraZeneca', 'astrazeneca')]:
    url = f'https://www.linkedin.com/company/{slug}/about'
    try:
        resp = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
        print(f'{company}: status={resp.status_code} final_url={resp.url!r}')
        if "authwall" in resp.url:
             print("  Hit authwall")
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            # Check JSON-LD
            for script in soup.find_all('script', {'type': 'application/ld+json'}):
                try:
                    data = json.loads(script.string or '')
                    if isinstance(data, dict):
                        if 'numberOfEmployees' in data:
                            print(f'  JSON-LD employees: {data["numberOfEmployees"]}')
                except: pass
            # Regex on text
            text = soup.get_text(' ', strip=True)
            match = re.search(r'([\d,]+\+?)\s*employees', text, re.IGNORECASE)
            if match:
                print(f'  Regex match: {match.group(0)}')
            else:
                print(f'  No employee count found in text (len={len(text)})')
                print(f'  Text preview: {text[:300]!r}')
    except Exception as e:
        print(f'{company}: ERROR {e}')
