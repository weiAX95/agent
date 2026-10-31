import requests

def search_web(query: str):
    response = requests.get(
        "http://localhost:8080/search",
        params={
            "q": query,
            "format": "json",
        },
        timeout=10,
    )

    response.raise_for_status()

    data = response.json()

    return [
        {
            "title": item["title"],
            "url": item["url"],
            "content": item.get("content", ""),
        }
        for item in data.get("results", [])
    ]


results = search_web("LangChain")

for result in results:
    print(result)