import requests
from urllib.parse import parse_qs, unquote, urlparse

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
}

for slug in ['abbott-laboratories', 'abbott']:
    url = f'https://www.linkedin.com/company/{slug}/'
    try:
        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        print(f'slug={slug!r}  status={resp.status_code}  final_url={resp.url!r}')
        if "authwall" in resp.url:
            qs = parse_qs(urlparse(resp.url).query)
            ref = unquote(qs.get("referrerUrl", [""])[0])
            print(f'  referrerUrl={ref!r}')
            print(f'  /company/{slug} in ref: {"/company/" + slug in ref}')
    except Exception as e:
        print(f'slug={slug!r}  ERROR: {e}')
