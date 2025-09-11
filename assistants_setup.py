# assistants_setup.py
# Создаёт ассистента с File Search + кастомными tools web_search / web_fetch и загружает резюме в Vector Store.
# usage:
#   python3 assistants_setup.py data/CVTimurAsyaev.pdf "Цифровой аватар Тимура Асяева"
#
# Выведет assistant_id -> добавьте в .env как OPENAI_ASSISTANT_ID=...

import sys, os
from openai import OpenAI
from config import settings

WEB_SEARCH_FN = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Поиск в интернете. Используй, если в файлах нет фактов или нужна актуализация. "
            "Запрос формулируй максимально точным. Возвращай 3–5 релевантных ссылок."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5}
            },
            "required": ["query"]
        }
    }
}

WEB_FETCH_FN = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Загрузка и первичное извлечение текста веб-страницы по URL. "
            "Вызывай это после web_search для выбранных ссылок."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL для скачивания"},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 12000, "default": 4000}
            },
            "required": ["url"]
        }
    }
}

INSTRUCTIONS = (
    "Ты — цифровой аватар Тимура Асяева. Алгоритм ответа:\n"
    "1) Сначала используй File Search (резюме) и опирайся на факты из файлов.\n"
    "2) Если в файлах нет достаточных фактов или нужна актуализация — вызови tool `web_search`, "
    "затем при необходимости `web_fetch` для выбранных ссылок. В ответе явно укажи источники.\n"
    "3) Для вопросов о размере компании: сначала попытайся извлечь из файлов название текущего работодателя, "
    "затем ищи headcount/численность по этой компании. Если файлов не хватает — честно скажи об этом и покажи источники из веба.\n"
    "4) Не выдумывай. Если даже веб не помог — честно скажи и предложи связаться по контактам владельца."
)

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 assistants_setup.py <CV.pdf> [Assistant name]")
        sys.exit(1)
    pdf_path = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) >= 3 else "Timur Asyaev CV Avatar"
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(pdf_path)

    client = OpenAI(api_key=settings.openai_api_key)

    # 1) Vector Store + загрузка файла
    vs = client.beta.vector_stores.create(name=f"Resume Store - {name}")
    with open(pdf_path, "rb") as f:
        client.beta.vector_stores.file_batches.upload_and_poll(
            vector_store_id=vs.id, files=[f]
        )

    # 2) Ассистент с file_search + двумя function-tools
    assistant = client.beta.assistants.create(
        name=name,
        model=settings.openai_model,
        instructions=INSTRUCTIONS,
        tools=[{"type": "file_search"}, WEB_SEARCH_FN, WEB_FETCH_FN],
        tool_resources={"file_search": {"vector_store_ids": [vs.id]}}
    )

    print("assistant_id:", assistant.id)
    print("vector_store_id:", vs.id)
    print("\nДобавьте в .env:\nOPENAI_ASSISTANT_ID=" + assistant.id)

if __name__ == "__main__":
    main()
