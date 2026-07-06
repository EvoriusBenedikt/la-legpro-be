import sys

lines = open('api/main.py', encoding='utf-8').readlines()
new_lines = []

for i, line in enumerate(lines):
    # Chat: 1338-1425
    # Repo: 1425-1781
    # KG: 1781-1970 (repo/failed is at 1970, wait, repo/failed was inside kg's range in my script? No, my script went up to 1970. Repo/failed is at 1970. Let's keep repo/failed in main for now or move it if needed).
    if 1338 <= i < 1970:
        continue
    new_lines.append(line)

# Add imports
for i, line in enumerate(new_lines):
    if 'from routers import admin, engineer' in line:
        new_lines.insert(i + 1, 'from routers import chat, repository, knowledge_graph\n')
        break

# Add mounts
for i, line in reversed(list(enumerate(new_lines))):
    if 'app.include_router(admin.router)' in line:
        new_lines.insert(i + 1, 'app.include_router(chat.router)\napp.include_router(repository.router)\napp.include_router(knowledge_graph.router)\n')
        break

with open('api/main.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
