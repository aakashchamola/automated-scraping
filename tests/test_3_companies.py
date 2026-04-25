import sys
from company_enricher import get_company_website, find_career_page

data = [
    ('Verana Health', 'https://www.linkedin.com/company/verana-health/'\),
    ('AstraZeneca', 'https://www.linkedin.com/company/astrazeneca/'\),
    ('Microsoft', 'https://www.linkedin.com/company/microsoft/'\),
]

for company, url in data:
    try:
        site = get_company_website(url)
        print(f'{company}: website={site!r}')
        if site:
            career = find_career_page(company, url)
            print(f'  career_page={career!r}')
    except Exception as e:
        print(f'ERROR for {company}: {e}')
