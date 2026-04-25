from company_enricher import get_company_website, find_career_page
test_data = [
    ('Microsoft', 'https://www.linkedin.com/company/microsoft/'\),
    ('Verana Health', 'https://www.linkedin.com/company/verana-health/'\),
    ('AstraZeneca', 'https://www.linkedin.com/company/astrazeneca/'\),
]
for company, url in test_data:
    site = get_company_website(url)
    career = find_career_page(company, url)
    print(f'{company}: website={site!r}  career={career!r}')
