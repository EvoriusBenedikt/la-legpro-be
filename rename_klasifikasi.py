import os

def replace_in_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    content = content.replace('"Publik"', '"Umum"')
    content = content.replace("'Publik'", "'Umum'")
    content = content.replace('Publik:', 'Umum:') # For JS objects
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

replace_in_file('api/main.py')
replace_in_file('../la-legpro-fe/src/components/AdminDashboard.tsx')
