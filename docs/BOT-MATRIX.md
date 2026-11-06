# Приложения

| Каталог | Python package | Зависимости | Проверки |
| --- | --- | --- | --- |
| apps/bots/serverbot | serverbot | pyproject.toml + uv.lock | pytest |
| apps/bots/chatbot | potato_bot | requirements lock | pytest |
| apps/bots/workbot | botwork | requirements с hashes | unittest |
| apps/bots/agentbot | — | — | Только заготовка |

WorkBot сохраняет серверную службу и модуль botwork. Каждый бот имеет отдельные секреты, данные и процесс.
