from company_enricher import get_company_website, find_career_page
for comp, url in [('Microsoft', 'https://www.linkedin.com/company/microsoft/')]:
  print(f'{comp}: {get_company_website(url)}')
