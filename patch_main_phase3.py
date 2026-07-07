import sys

lines = open('api/main.py', encoding='utf-8').readlines()
new_lines = []

for i, line in enumerate(lines):
    if 1368 <= i < 2019:
        continue
    new_lines.append(line)

# Add imports
for i, line in enumerate(new_lines):
    if 'from routers import chat, repository, knowledge_graph' in line:
        new_lines.insert(i + 1, 'from routers import compliance\n')
        break

# Add mounts
for i, line in reversed(list(enumerate(new_lines))):
    if 'app.include_router(knowledge_graph.router)' in line:
        new_lines.insert(i + 1, 'app.include_router(compliance.router)\n')
        break

with open('api/main.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
