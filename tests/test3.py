from company_enricher import get_company_website, find_career_page
for c, u in [('Verana Health', 'https://www.linkedin.com/company/verana-health/'), ('AstraZeneca', 'https://www.linkedin.com/company/astrazeneca/'), ('Microsoft', 'https://www.linkedin.com/company/microsoft/')]:
    site = get_company_website(u)
    print(f'{c}: website={site!r}')
    if site:
        print(f'  career_page={find_career_page(c, u)!r}')
