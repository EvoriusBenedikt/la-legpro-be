import sys

lines = open('api/main.py', encoding='utf-8').readlines()

new_lines = []
skip = False

# We need to skip:
# 1935 to 2025 (Admin Dashboard)
# 2736 to 2901 (Admin exclusions + Engineer routes)

for i, line in enumerate(lines):
    if 1935 <= i < 2025:
        continue
    if 2736 <= i < 2901:
        continue
    new_lines.append(line)

# Now we need to insert the router inclusions
# Find where the other includes or FastAPI init happens.
# Line 13 has FastAPI import. Line 16 has pydantic.
# We'll just insert router imports after `import auth` which is around line 17.

for i, line in enumerate(new_lines):
    if line.startswith('import auth'):
        new_lines.insert(i + 1, 'from routers import admin, engineer\n')
        break

# Now find where we mount routers. We don't have routers mounted yet. 
# We'll mount them right before `if __name__ == "__main__":`

for i, line in reversed(list(enumerate(new_lines))):
    if 'if __name__ == "__main__":' in line:
        new_lines.insert(i, '\napp.include_router(admin.router)\napp.include_router(engineer.router)\n\n')
        break

with open('api/main.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
